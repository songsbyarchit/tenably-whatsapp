import os
import re
import json
import uuid
import logging
import sys
import time as time_module
from urllib.parse import urlencode
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo
from flask import Flask, request, send_file, jsonify
from twilio.rest import Client as TwilioClient
from dotenv import load_dotenv
import anthropic
import requests as http_requests
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

load_dotenv()

app = Flask(__name__)
app.logger.setLevel(logging.INFO)
if not app.logger.handlers:
    app.logger.addHandler(logging.StreamHandler(sys.stdout))

client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
twilio_client = TwilioClient(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"])

CALENDAR_ID = os.environ.get("GOOGLE_CALENDAR_ID", "primary")
TIMEZONE = ZoneInfo(os.environ.get("CALENDAR_TIMEZONE", "Europe/London"))
SLOT_DURATION_HOURS = 0.5
DAY_START = time(9, 0)
DAY_END = time(21, 0)
LANDLORD_EMAIL = "archit.sachdeva@gmail.com"
RENTER_EMAIL = "archit.sachdeva007@gmail.com"
RENTER_HOME_LOCATION = os.environ.get("RENTER_HOME_LOCATION", "Old Street Station London")
VIEWING_PROPERTY_ADDRESS = os.environ.get("VIEWING_PROPERTY_ADDRESS", "")
GOOGLE_MAPS_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY", "")
TWILIO_WHATSAPP_FROM = os.environ.get("TWILIO_WHATSAPP_FROM", "")
WEBHOOK_BASE_URL = os.environ.get("WEBHOOK_BASE_URL", "").rstrip("/")
PREFS_FILE = "renter_preferences.json"
RENTER_NAME  = os.environ.get("RENTER_NAME",  "the renter")
RENTER_AREA  = os.environ.get("RENTER_AREA",  "")
RENTER_PHONE = os.environ.get("RENTER_PHONE", "")

# In-memory conversation history keyed by sender phone number
conversation_history: dict[str, list[dict]] = {}

# Pending viewings awaiting renter acceptance: event_id -> landlord phone
pending_viewings: dict[str, str] = {}
confirmed_viewings: set[str] = set()
_watch_channel_id: str | None = None

# Senders that have already received the intro media files
intro_media_sent: set[str] = set()

DOCUMENT_MAP = {
    "payslip":       os.environ.get("PAYSLIP_URL", ""),
    "right_to_rent": os.environ.get("RIGHT_TO_RENT_URL", ""),
    "passport":      os.environ.get("PASSPORT_URL", ""),
}

# Tool definition for Claude
TOOLS = [
    {
        "name": "check_slot_availability",
        "description": (
            "Check whether a specific time slot is free in the renter's calendar. "
            "Always call this before confirming any time the landlord proposes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "start_iso": {
                    "type": "string",
                    "description": "Proposed slot start in ISO 8601 format, e.g. 2024-06-01T10:00:00",
                },
            },
            "required": ["start_iso"],
        },
    },
    {
        "name": "send_documents",
        "description": (
            "Send renter documents to the landlord via WhatsApp media messages. "
            "Call this whenever the landlord asks to see any documents. "
            "Infer which documents they want — 'payslip', 'right_to_rent', 'passport'. "
            "Send all three for broad requests like 'send all docs' or 'send everything'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "documents": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": ["payslip", "right_to_rent", "passport"],
                    },
                    "description": "List of documents to send.",
                },
            },
            "required": ["documents"],
        },
    },
    {
        "name": "create_viewing_event",
        "description": (
            "Create a Google Calendar event for a confirmed property viewing. "
            "Only call this after check_slot_availability confirms the slot is free."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "start_iso": {
                    "type": "string",
                    "description": "Event start in ISO 8601 format, e.g. 2024-06-01T10:00:00",
                },
                "end_iso": {
                    "type": "string",
                    "description": "Event end in ISO 8601 format, e.g. 2024-06-01T11:00:00",
                },
            },
            "required": ["start_iso", "end_iso"],
        },
    },
]


def load_preferences() -> dict:
    try:
        with open(PREFS_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"day_start": "09:00", "day_end": "21:00", "blocked_slots": []}


def load_property_address() -> str:
    import os.path
    if os.path.exists("property.json"):
        try:
            with open("property.json") as f:
                raw = f.read()
            app.logger.info(f"load_property_address: property.json contents: {raw!r}")
            address = json.loads(raw).get("address", "")
            if address:
                app.logger.info(f"load_property_address: using address from property.json: {address!r}")
                return address
            app.logger.warning("load_property_address: property.json exists but 'address' key is missing or empty")
        except json.JSONDecodeError as e:
            app.logger.warning(f"load_property_address: failed to parse property.json: {e}")
    else:
        app.logger.warning("load_property_address: property.json not found on disk")

    fallback = os.environ.get("PROPERTY_ADDRESS") or VIEWING_PROPERTY_ADDRESS
    if fallback:
        app.logger.info(f"load_property_address: using fallback address: {fallback!r}")
    else:
        app.logger.warning("load_property_address: no property address available — set PROPERTY_ADDRESS env var")
    return fallback


def send_documents(doc_names: list[str], to: str) -> str:
    """Send requested renter documents as WhatsApp media messages."""
    if not TWILIO_WHATSAPP_FROM:
        return "Cannot send documents — TWILIO_WHATSAPP_FROM not set."
    to_wa   = to if to.startswith("whatsapp:") else f"whatsapp:{to}"
    from_wa = TWILIO_WHATSAPP_FROM if TWILIO_WHATSAPP_FROM.startswith("whatsapp:") else f"whatsapp:{TWILIO_WHATSAPP_FROM}"
    sent = []
    for name in doc_names:
        url = DOCUMENT_MAP.get(name)
        if not url:
            app.logger.warning(f"send_documents: no URL configured for {name!r} — set {name.upper()}_URL env var")
            continue
        twilio_client.messages.create(from_=from_wa, to=to_wa, media_url=[url])
        sent.append(name)
    return f"Sent: {', '.join(sent)}." if sent else "No valid documents specified."


def send_intro_messages(to: str) -> None:
    """Send five ordered intro texts before any other message."""
    first = os.environ.get("RENTER_NAME", "the renter").split()[0]
    area  = os.environ.get("RENTER_AREA", "")
    phone = os.environ.get("RENTER_PHONE", "")
    messages = [
        "Hi I'm an AI assistant from Tenably helping landlords and renters arrange viewings",
        f"I'm reaching out on behalf of {first}",
        f"{first} currently lives in {area} and is interested in your property",
        f"If you'd prefer to speak directly you can call or text them at {phone}",
        f"I already have {first}'s payslip right to rent and passport — just ask if you'd like any of them",
    ]
    for i, msg in enumerate(messages):
        if i > 0:
            time_module.sleep(1)
        send_whatsapp(to, msg)


def send_whatsapp(to: str, body: str) -> None:
    """Send an outbound WhatsApp message via Twilio."""
    if not TWILIO_WHATSAPP_FROM:
        app.logger.warning("TWILIO_WHATSAPP_FROM not set — skipping WhatsApp send")
        return
    to_wa = to if to.startswith("whatsapp:") else f"whatsapp:{to}"
    twilio_client.messages.create(
        from_=TWILIO_WHATSAPP_FROM if TWILIO_WHATSAPP_FROM.startswith("whatsapp:") else f"whatsapp:{TWILIO_WHATSAPP_FROM}",
        to=to_wa,
        body=body.replace("\n", " ").strip(),
    )


def setup_calendar_watch() -> None:
    """Register a Google Calendar push-notification channel (valid for 7 days)."""
    global _watch_channel_id
    if not WEBHOOK_BASE_URL:
        return
    service = get_calendar_service()
    channel_id = str(uuid.uuid4())
    expiry_ms = int((datetime.now(tz=TIMEZONE) + timedelta(days=7)).timestamp() * 1000)
    service.events().watch(
        calendarId=CALENDAR_ID,
        body={
            "id": channel_id,
            "type": "web_hook",
            "address": f"{WEBHOOK_BASE_URL}/calendar-webhook",
            "expiration": str(expiry_ms),
        },
    ).execute()
    _watch_channel_id = channel_id


def get_commute_minutes(origin: str, destination: str, departure_time: datetime) -> int:
    """Return travel time in minutes between two locations using Google Maps Distance Matrix API.
    Falls back to 0 if the API key is missing or the request fails."""
    if not GOOGLE_MAPS_API_KEY:
        app.logger.warning("GOOGLE_MAPS_API_KEY not set — commute check skipped, assuming 0 mins")
        return 0
    if not origin or not destination:
        app.logger.warning(f"get_commute_minutes: missing origin={origin!r} or destination={destination!r}")
        return 0
    data = None
    try:
        resp = http_requests.get(
            "https://maps.googleapis.com/maps/api/distancematrix/json",
            params={
                "origins": origin,
                "destinations": destination,
                "mode": "transit",
                "departure_time": int(departure_time.timestamp()),
                "key": GOOGLE_MAPS_API_KEY,
            },
            timeout=5,
        )
        data = resp.json()
        top_status = data.get("status")
        if top_status != "OK":
            app.logger.warning(f"Distance Matrix top-level status={top_status!r} | full response: {data}")
            return 0
        rows = data.get("rows", [])
        if not rows or not rows[0].get("elements"):
            app.logger.warning(f"Distance Matrix empty rows/elements | full response: {data}")
            return 0
        element = rows[0]["elements"][0]
        if element["status"] == "OK":
            minutes = element["duration"]["value"] // 60
            app.logger.info(f"Commute | {origin!r} → {destination!r} @ {departure_time.strftime('%-I:%M %p')} = {minutes} mins")
            return minutes
        app.logger.warning(f"Distance Matrix element status={element['status']!r} | {origin!r} → {destination!r} | full response: {data}")
    except Exception as e:
        app.logger.warning(f"Distance Matrix API error: {e} | {origin!r} → {destination!r} | response: {data}")
    return 0


_UK_POSTCODE_RE = re.compile(
    r"\b[A-Z]{1,2}[0-9][0-9A-Z]?\s?[0-9][A-Z]{2}\b", re.IGNORECASE
)
_UK_KEYWORDS = (
    "uk", "united kingdom", "england", "scotland", "wales",
    "london", "manchester", "birmingham", "leeds", "bristol",
    "edinburgh", "glasgow", "liverpool", "sheffield", "oxford",
    "cambridge", "nottingham", "leicester", "coventry", "newcastle",
)

def is_valid_uk_location(loc: str) -> bool:
    """Return True if loc looks like a real UK address suitable for routing."""
    if not loc or not loc.strip():
        return False
    lower = loc.lower()
    if any(kw in lower for kw in _UK_KEYWORDS):
        return True
    if _UK_POSTCODE_RE.search(loc):
        return True
    return False


def get_calendar_service():
    token_json = os.environ.get("GOOGLE_TOKEN_JSON")
    if token_json:
        creds = Credentials.from_authorized_user_info(json.loads(token_json))
    else:
        creds = Credentials.from_authorized_user_file("token.json")
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        if not token_json:
            with open("token.json", "w") as f:
                f.write(creds.to_json())
    return build("calendar", "v3", credentials=creds)


def get_free_slots() -> str:
    """Fetch free 30-minute slots over the next 7 days from Google Calendar.
    Respects the renter's viewing window and blocked slots. Accounts for commute time."""
    prefs = load_preferences()
    day_start = time.fromisoformat(prefs.get("day_start", "09:00"))
    day_end   = time.fromisoformat(prefs.get("day_end",   "21:00"))

    service = get_calendar_service()
    now = datetime.now(tz=TIMEZONE)
    week_end = now + timedelta(days=7)

    events_result = service.events().list(
        calendarId=CALENDAR_ID,
        timeMin=now.isoformat(),
        timeMax=week_end.isoformat(),
        singleEvents=True,
        orderBy="startTime",
    ).execute()

    busy: list[tuple[datetime, datetime, str, str]] = []  # (start, end, location, name)
    for event in events_result.get("items", []):
        start = event["start"].get("dateTime") or event["start"].get("date")
        end = event["end"].get("dateTime") or event["end"].get("date")
        raw_location = event.get("location", "")
        location = raw_location if is_valid_uk_location(raw_location) else ""
        name = event.get("summary", "(unnamed event)")
        if raw_location and not location:
            app.logger.info(f"get_free_slots: ignoring non-UK/invalid location {raw_location!r} for event {name!r} — falling back to home")
        try:
            busy.append((
                datetime.fromisoformat(start).astimezone(TIMEZONE),
                datetime.fromisoformat(end).astimezone(TIMEZONE),
                location,
                name,
            ))
        except ValueError:
            continue

    # Expand recurring blocked slots (from prefs) into the 7-day window
    for day_offset in range(8):
        d = (now + timedelta(days=day_offset)).date()
        for bs in prefs.get("blocked_slots", []):
            try:
                busy.append((
                    datetime.combine(d, time.fromisoformat(bs["start"]), tzinfo=TIMEZONE),
                    datetime.combine(d, time.fromisoformat(bs["end"]),   tzinfo=TIMEZONE),
                    "",
                    "blocked slot",
                ))
            except (KeyError, ValueError):
                continue

    def preceding_event(slot_start: datetime) -> tuple[datetime, str, str]:
        """Return the end time, location, and name of the event immediately before slot_start."""
        preceding = [e for e in busy if e[1] <= slot_start]
        if not preceding:
            return slot_start, RENTER_HOME_LOCATION, "home"
        latest = max(preceding, key=lambda e: e[1])
        return latest[1], latest[2] or RENTER_HOME_LOCATION, latest[3]

    def following_event(slot_end: datetime) -> tuple[datetime, str, str] | None:
        """Return the start time, location, and name of the event immediately after slot_end."""
        following = [e for e in busy if e[0] >= slot_end]
        if not following:
            return None
        earliest = min(following, key=lambda e: e[0])
        return earliest[0], earliest[2] or RENTER_HOME_LOCATION, earliest[3]

    property_address = load_property_address()
    if not property_address:
        app.logger.warning("get_free_slots: no property address found — commute checks will be skipped")

    free_slots: list[str] = []
    for day_offset in range(7):
        day = (now + timedelta(days=day_offset)).date()
        slot_start = datetime.combine(day, day_start, tzinfo=TIMEZONE)
        day_end_dt = datetime.combine(day, day_end,   tzinfo=TIMEZONE)

        while slot_start + timedelta(hours=SLOT_DURATION_HOURS) <= day_end_dt:
            slot_end = slot_start + timedelta(hours=SLOT_DURATION_HOURS)
            slot_label = slot_start.strftime("%-d %b %-I:%M %p")

            if slot_end <= now:
                slot_start = slot_end
                continue

            is_free = all(
                slot_end <= b_start or slot_start >= b_end
                for b_start, b_end, *_ in busy
            )

            if not is_free:
                clashing = [e for e in busy if not (slot_end <= e[0] or slot_start >= e[1])]
                clash_names = ", ".join(e[3] for e in clashing) or "unknown"
                app.logger.info(f"Slot {slot_label} | BUSY — clashes with: {clash_names}")
                slot_start = slot_end
                continue

            if property_address:
                # Inbound: can renter arrive from preceding event in time?
                event_end_time, origin, prev_name = preceding_event(slot_start)
                inbound_minutes = get_commute_minutes(origin, property_address, event_end_time)
                latest_arrival = event_end_time + timedelta(minutes=inbound_minutes)
                app.logger.info(
                    f"Slot {slot_label} | inbound: prev_event={prev_name!r} origin={origin!r} "
                    f"ends={event_end_time.strftime('%-I:%M %p')} commute={inbound_minutes}m "
                    f"arrives={latest_arrival.strftime('%-I:%M %p')} need_by={slot_start.strftime('%-I:%M %p')}"
                )
                if latest_arrival > slot_start:
                    app.logger.info(
                        f"Slot {slot_label} | REJECTED — inbound clash: {inbound_minutes}m from {prev_name!r} "
                        f"({origin!r}) arrives {latest_arrival.strftime('%-I:%M %p')}, slot starts {slot_start.strftime('%-I:%M %p')}"
                    )
                    slot_start = slot_end
                    continue

                # Outbound: can renter reach the next event from the viewing in time?
                next_ev = following_event(slot_end)
                if next_ev:
                    next_start, next_location, next_name = next_ev
                    outbound_minutes = get_commute_minutes(property_address, next_location, slot_end)
                    earliest_arrival = slot_end + timedelta(minutes=outbound_minutes)
                    app.logger.info(
                        f"Slot {slot_label} | outbound: next_event={next_name!r} dest={next_location!r} "
                        f"departs={slot_end.strftime('%-I:%M %p')} commute={outbound_minutes}m "
                        f"arrives={earliest_arrival.strftime('%-I:%M %p')} need_by={next_start.strftime('%-I:%M %p')}"
                    )
                    if earliest_arrival > next_start:
                        app.logger.info(
                            f"Slot {slot_label} | REJECTED — outbound clash: {outbound_minutes}m to {next_name!r} "
                            f"({next_location!r}) arrives {earliest_arrival.strftime('%-I:%M %p')}, event starts {next_start.strftime('%-I:%M %p')}"
                        )
                        slot_start = slot_end
                        continue
                else:
                    app.logger.info(f"Slot {slot_label} | outbound: no following event — skipped")

            app.logger.info(f"Slot {slot_label} | AVAILABLE")
            free_slots.append(
                slot_start.strftime("%-d %b (%a) %-I:%M %p")
                + " – "
                + slot_end.strftime("%-I:%M %p")
                + f" | ISO: {slot_start.strftime('%Y-%m-%dT%H:%M:%S')} / {slot_end.strftime('%Y-%m-%dT%H:%M:%S')}"
            )
            slot_start = slot_end

    if not free_slots:
        return "No available slots in the next 7 days."
    return "\n".join(f"- {s}" for s in free_slots)


def check_slot_availability(start_iso: str) -> str:
    """Check if a 30-minute slot is free and commute-feasible; suggest nearest valid slot if not."""
    prefs = load_preferences()
    day_start        = time.fromisoformat(prefs.get("day_start", "09:00"))
    day_end          = time.fromisoformat(prefs.get("day_end",   "21:00"))
    property_address = load_property_address()

    service    = get_calendar_service()
    slot_start = datetime.fromisoformat(start_iso).replace(tzinfo=TIMEZONE)
    slot_end   = slot_start + timedelta(hours=SLOT_DURATION_HOURS)

    now = datetime.now(tz=TIMEZONE)
    if slot_start < now:
        return "That time has already passed."
    if slot_start.time() < day_start or slot_end.time() > day_end:
        return (
            f"Outside allowed hours "
            f"({day_start.strftime('%-I:%M %p')}–{day_end.strftime('%-I:%M %p')}). "
            "Please pick a time within that window."
        )

    window_end = now + timedelta(days=7)
    search_end = max(slot_end, window_end)

    events_result = service.events().list(
        calendarId=CALENDAR_ID,
        timeMin=now.isoformat(),
        timeMax=search_end.isoformat(),
        singleEvents=True,
        orderBy="startTime",
    ).execute()

    busy: list[tuple[datetime, datetime, str]] = []  # (start, end, location)
    for event in events_result.get("items", []):
        start        = event["start"].get("dateTime") or event["start"].get("date")
        end          = event["end"].get("dateTime") or event["end"].get("date")
        raw_location = event.get("location", "")
        location     = raw_location if is_valid_uk_location(raw_location) else ""
        if raw_location and not location:
            app.logger.info(f"check_slot_availability: ignoring non-UK/invalid location {raw_location!r} — falling back to home")
        try:
            busy.append((
                datetime.fromisoformat(start).astimezone(TIMEZONE),
                datetime.fromisoformat(end).astimezone(TIMEZONE),
                location,
            ))
        except ValueError:
            continue

    for day_offset in range(8):
        d = (now + timedelta(days=day_offset)).date()
        for bs in prefs.get("blocked_slots", []):
            try:
                busy.append((
                    datetime.combine(d, time.fromisoformat(bs["start"]), tzinfo=TIMEZONE),
                    datetime.combine(d, time.fromisoformat(bs["end"]),   tzinfo=TIMEZONE),
                    "",
                ))
            except (KeyError, ValueError):
                continue

    def preceding_loc(s: datetime) -> tuple[datetime, str]:
        pre = [e for e in busy if e[1] <= s]
        if not pre:
            return s, RENTER_HOME_LOCATION
        latest = max(pre, key=lambda e: e[1])
        return latest[1], latest[2] or RENTER_HOME_LOCATION

    def following_loc(e: datetime) -> tuple[datetime, str] | None:
        fol = [ev for ev in busy if ev[0] >= e]
        if not fol:
            return None
        earliest = min(fol, key=lambda ev: ev[0])
        return earliest[0], earliest[2] or RENTER_HOME_LOCATION

    def is_calendar_free(s: datetime) -> bool:
        e = s + timedelta(hours=SLOT_DURATION_HOURS)
        return all(e <= b_start or s >= b_end for b_start, b_end, _ in busy)

    def is_commute_ok(s: datetime) -> bool:
        if not property_address:
            return True
        e = s + timedelta(hours=SLOT_DURATION_HOURS)
        ev_end, origin = preceding_loc(s)
        if ev_end + timedelta(minutes=get_commute_minutes(origin, property_address, ev_end)) > s:
            return False
        nxt = following_loc(e)
        if nxt:
            nxt_start, nxt_loc = nxt
            if e + timedelta(minutes=get_commute_minutes(property_address, nxt_loc, e)) > nxt_start:
                return False
        return True

    def fmt(s: datetime) -> str:
        return s.strftime("%-I:%M %p")

    if is_calendar_free(slot_start) and is_commute_ok(slot_start):
        return (
            f"Free. {fmt(slot_start)} to {fmt(slot_end)}. "
            f"start_iso={slot_start.strftime('%Y-%m-%dT%H:%M:%S')} "
            f"end_iso={slot_end.strftime('%Y-%m-%dT%H:%M:%S')}"
        )

    # Find nearest slot that is both calendar-free and commute-feasible
    candidate = datetime.combine(now.date(), day_start, tzinfo=TIMEZONE)
    while candidate + timedelta(hours=SLOT_DURATION_HOURS) <= window_end:
        c_end = candidate + timedelta(hours=SLOT_DURATION_HOURS)
        if c_end > now and candidate.time() >= day_start and c_end.time() <= day_end:
            if is_calendar_free(candidate) and is_commute_ok(candidate):
                return (
                    f"Busy. Nearest free slot: {candidate.strftime('%-d %b (%a)')} "
                    f"{fmt(candidate)} to {fmt(c_end)} | "
                    f"start_iso={candidate.strftime('%Y-%m-%dT%H:%M:%S')} "
                    f"end_iso={c_end.strftime('%Y-%m-%dT%H:%M:%S')}"
                )
        candidate += timedelta(hours=SLOT_DURATION_HOURS)
        if candidate.time() >= day_end:
            next_day = (candidate + timedelta(days=1)).date()
            candidate = datetime.combine(next_day, day_start, tzinfo=TIMEZONE)

    return "Busy and no free slots found in the next 7 days."


def create_viewing_event(start_iso: str, end_iso: str, landlord_phone: str) -> str:
    """Create a tentative calendar invite for the renter to accept."""
    global _watch_channel_id
    service = get_calendar_service()
    tz_str = str(TIMEZONE)

    start_dt = datetime.fromisoformat(start_iso).replace(tzinfo=TIMEZONE)
    friendly = start_dt.strftime("%-d %B %Y at %-I:%M %p")
    property_address = load_property_address()
    property_line = f"\nProperty: {property_address}" if property_address else ""

    event = {
        "summary": "Property Viewing",
        "description": f"Viewing arranged via Tenably\nDate/Time: {friendly}{property_line}\n\nPlease accept or decline this invite.\n\nPlease reply Yes to this invite as soon as you know you're free — your Tenably agent is waiting to confirm with the landlord",
        "start": {"dateTime": start_iso, "timeZone": tz_str},
        "end": {"dateTime": end_iso, "timeZone": tz_str},
        "status": "tentative",
        "attendees": [
            {"email": LANDLORD_EMAIL},
            {"email": RENTER_EMAIL},
        ],
        "sendUpdates": "all",
    }
    created = service.events().insert(calendarId=CALENDAR_ID, body=event).execute()
    pending_viewings[created["id"]] = landlord_phone

    if not _watch_channel_id:
        try:
            setup_calendar_watch()
        except Exception as e:
            app.logger.warning(f"Could not set up calendar watch: {e}")

    end_dt = datetime.fromisoformat(end_iso).replace(tzinfo=TIMEZONE)
    cal_link = "https://calendar.google.com/calendar/render?" + urlencode({
        "action": "TEMPLATE",
        "text": "Property Viewing",
        "dates": start_dt.strftime("%Y%m%dT%H%M%S") + "/" + end_dt.strftime("%Y%m%dT%H%M%S"),
        "details": f"Viewing arranged via Tenably{property_line}",
    })
    send_whatsapp(landlord_phone, f"Add to your calendar: {cal_link}")

    renter_first = RENTER_NAME.split()[0]
    return f"Invite sent to renter. Reply to the landlord with exactly: Just confirming with {renter_first} now — will get back to you shortly"


def build_system_prompt() -> str:
    available_slots = get_free_slots()
    now_str = datetime.now(tz=TIMEZONE).strftime("%A %-d %B %Y, %-I:%M %p %Z")
    renter_first = RENTER_NAME.split()[0]
    return f"""You are a WhatsApp assistant for Tenably arranging a property viewing on behalf of {RENTER_NAME}.

Current date and time: {now_str}

Rules:
- Write in British English at all times.
- Max 20 words per reply. Casual, warm, human tone. No paragraphs, no bullet lists.
- No emojis. Minimal punctuation. No commas unless absolutely necessary. Exclamation marks only occasionally at the end of a sentence.
- Never say "we". Always use first name only: {renter_first}. Say "{renter_first} is free at..." not "we have availability". Never use the full name.
- Never list all available slots. Pick 2 or 3 of the soonest viable options and suggest them naturally in conversation.
- Only propose times from the available slots below. If the landlord suggests another time, call check_slot_availability first — if it's free confirm it, if not suggest the nearest free slot returned by the tool.
- When proposing or confirming any time always state the exact start and end time in the format "Xpm to Ypm" e.g. "6:30pm to 7:00pm". Never mention just a start time without the end time.
- If the landlord asks for any documents, call send_documents with the relevant names from: payslip, right_to_rent, passport. For broad requests like "all docs" or "everything" send all three. After sending, reply confirming which were sent.
- When a time is agreed and confirmed free, call create_viewing_event, then reply to the landlord with exactly: Just confirming with {renter_first} now — will get back to you shortly

Renter's available slots (ISO times included for tool use):
{available_slots}"""


def handle_message(body: str, sender: str) -> str:
    history = conversation_history.setdefault(sender, [])
    history.append({"role": "user", "content": body})

    # Agentic loop — keeps going until Claude stops calling tools
    while True:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=build_system_prompt(),
            tools=TOOLS,
            messages=history,
        )

        # Append assistant turn to history
        history.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            # Extract the text reply
            for block in response.content:
                if hasattr(block, "text"):
                    return block.text
            return ""

        # Handle tool calls
        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            if block.name == "check_slot_availability":
                result = check_slot_availability(block.input["start_iso"])
            elif block.name == "send_documents":
                result = send_documents(block.input["documents"], sender)
            elif block.name == "create_viewing_event":
                result = create_viewing_event(
                    block.input["start_iso"], block.input["end_iso"], sender
                )
            else:
                result = "Unknown tool."
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": result,
            })

        history.append({"role": "user", "content": tool_results})

@app.route("/googled9489acb4345354b.html")
def google_site_verification():
    with open("googled9489acb4345354b.html") as f:
        return f.read(), 200, {"Content-Type": "text/html"}


@app.route("/preferences")
def preferences_page():
    return send_file("preferences.html")


@app.route("/preferences/data", methods=["GET"])
def preferences_get():
    return jsonify(load_preferences())


@app.route("/preferences/data", methods=["POST"])
def preferences_save():
    data = request.get_json(force=True)
    with open(PREFS_FILE, "w") as f:
        json.dump(data, f, indent=2)
    return jsonify({"ok": True})


@app.route("/calendar-webhook", methods=["POST"])
def calendar_webhook():
    # Google sends a sync notification when the watch is first set up — ignore it
    if request.headers.get("X-Goog-Resource-State") == "sync":
        return "", 200

    if not pending_viewings:
        return "", 200

    try:
        service = get_calendar_service()
        for event_id, landlord_phone in list(pending_viewings.items()):
            if event_id in confirmed_viewings:
                continue
            event = service.events().get(calendarId=CALENDAR_ID, eventId=event_id).execute()
            for attendee in event.get("attendees", []):
                if attendee["email"] == RENTER_EMAIL and attendee.get("responseStatus") == "accepted":
                    start_str = event["start"].get("dateTime", "")
                    if start_str:
                        start_dt = datetime.fromisoformat(start_str).astimezone(TIMEZONE)
                        friendly = start_dt.strftime("%-d %B at %-I:%M %p")
                    else:
                        friendly = "the agreed time"
                    confirmed_viewings.add(event_id)
                    send_whatsapp(landlord_phone, f"Confirmed. Renter accepted the viewing on {friendly}")
                    del pending_viewings[event_id]
                    # Update event status to confirmed
                    service.events().patch(
                        calendarId=CALENDAR_ID,
                        eventId=event_id,
                        body={"status": "confirmed"},
                        sendUpdates="none",
                    ).execute()
                    break
    except Exception as e:
        app.logger.error(f"Calendar webhook error: {e}")

    return "", 200


@app.route("/webhook", methods=["POST"])
def webhook():
    body   = request.form.get("Body", "")
    sender = request.form.get("From", "")

    app.logger.info(f"Incoming message | from={sender} | body={body!r}")

    if body.strip().lower() == "reset":
        conversation_history.pop(sender, None)
        intro_media_sent.discard(sender)
        send_whatsapp(sender, "Ready. Send a message to start fresh.")
        return ("", 200)

    is_first = sender not in intro_media_sent

    # Compute the bot reply before sending anything so all outbound messages
    # are dispatched in the guaranteed order: intro texts → media → bot reply.
    try:
        reply = handle_message(body, sender)
    except Exception as e:
        app.logger.error(f"handle_message error: {e}", exc_info=True)
        return ("", 200)

    app.logger.info(f"Outgoing reply | to={sender} | body={reply!r}")

    if is_first:
        intro_media_sent.add(sender)
        try:
            send_intro_messages(sender)
        except Exception as e:
            app.logger.warning(f"Could not send intro messages: {e}")

    send_whatsapp(sender, reply)
    return ("", 200)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
