import os
import json
import uuid
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo
from flask import Flask, request, send_file, jsonify
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client as TwilioClient
from dotenv import load_dotenv
import anthropic
import requests as http_requests
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

load_dotenv()

app = Flask(__name__)
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

# In-memory conversation history keyed by sender phone number
conversation_history: dict[str, list[dict]] = {}

# Pending viewings awaiting renter acceptance: event_id -> landlord phone
pending_viewings: dict[str, str] = {}
_watch_channel_id: str | None = None

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


def send_whatsapp(to: str, body: str) -> None:
    """Send an outbound WhatsApp message via Twilio."""
    if not TWILIO_WHATSAPP_FROM:
        app.logger.warning("TWILIO_WHATSAPP_FROM not set — skipping WhatsApp send")
        return
    to_wa = to if to.startswith("whatsapp:") else f"whatsapp:{to}"
    twilio_client.messages.create(
        from_=f"whatsapp:{TWILIO_WHATSAPP_FROM}",
        to=to_wa,
        body=body,
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
    if not GOOGLE_MAPS_API_KEY or not origin or not destination:
        return 0
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
        element = data["rows"][0]["elements"][0]
        if element["status"] == "OK":
            return element["duration"]["value"] // 60
    except Exception:
        pass
    return 0


def get_calendar_service():
    creds = Credentials.from_authorized_user_file("token.json")
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
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

    busy: list[tuple[datetime, datetime, str]] = []  # (start, end, location)
    for event in events_result.get("items", []):
        start = event["start"].get("dateTime") or event["start"].get("date")
        end = event["end"].get("dateTime") or event["end"].get("date")
        location = event.get("location", "")
        try:
            busy.append((
                datetime.fromisoformat(start).astimezone(TIMEZONE),
                datetime.fromisoformat(end).astimezone(TIMEZONE),
                location,
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
                ))
            except (KeyError, ValueError):
                continue

    def preceding_event(slot_start: datetime) -> tuple[datetime, str]:
        """Return the end time and location of the event immediately before slot_start."""
        preceding = [e for e in busy if e[1] <= slot_start]
        if not preceding:
            return slot_start, RENTER_HOME_LOCATION
        latest = max(preceding, key=lambda e: e[1])
        return latest[1], latest[2] or RENTER_HOME_LOCATION

    def following_event(slot_end: datetime) -> tuple[datetime, str] | None:
        """Return the start time and location of the event immediately after slot_end."""
        following = [e for e in busy if e[0] >= slot_end]
        if not following:
            return None
        earliest = min(following, key=lambda e: e[0])
        return earliest[0], earliest[2] or RENTER_HOME_LOCATION

    free_slots: list[str] = []
    for day_offset in range(7):
        day = (now + timedelta(days=day_offset)).date()
        slot_start = datetime.combine(day, day_start, tzinfo=TIMEZONE)
        day_end_dt = datetime.combine(day, day_end,   tzinfo=TIMEZONE)

        while slot_start + timedelta(hours=SLOT_DURATION_HOURS) <= day_end_dt:
            slot_end = slot_start + timedelta(hours=SLOT_DURATION_HOURS)
            if slot_end <= now:
                slot_start = slot_end
                continue

            is_free = all(
                slot_end <= b_start or slot_start >= b_end
                for b_start, b_end, _ in busy
            )

            if is_free and VIEWING_PROPERTY_ADDRESS:
                # Check inbound commute: can renter arrive from preceding event in time?
                event_end_time, origin = preceding_event(slot_start)
                inbound_minutes = get_commute_minutes(origin, VIEWING_PROPERTY_ADDRESS, event_end_time)
                if event_end_time + timedelta(minutes=inbound_minutes) > slot_start:
                    slot_start = slot_end
                    continue

                # Check outbound commute: can renter reach the next event from the viewing in time?
                next_event = following_event(slot_end)
                if next_event:
                    next_start, next_location = next_event
                    outbound_minutes = get_commute_minutes(VIEWING_PROPERTY_ADDRESS, next_location, slot_end)
                    if slot_end + timedelta(minutes=outbound_minutes) > next_start:
                        slot_start = slot_end
                        continue

            if is_free:
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
    """Check if a 30-minute slot is free; if not, return the nearest free slot."""
    prefs = load_preferences()
    day_start = time.fromisoformat(prefs.get("day_start", "09:00"))
    day_end   = time.fromisoformat(prefs.get("day_end",   "21:00"))

    service = get_calendar_service()
    slot_start = datetime.fromisoformat(start_iso).replace(tzinfo=TIMEZONE)
    slot_end = slot_start + timedelta(hours=SLOT_DURATION_HOURS)

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

    busy: list[tuple[datetime, datetime]] = []
    for event in events_result.get("items", []):
        start = event["start"].get("dateTime") or event["start"].get("date")
        end = event["end"].get("dateTime") or event["end"].get("date")
        try:
            busy.append((
                datetime.fromisoformat(start).astimezone(TIMEZONE),
                datetime.fromisoformat(end).astimezone(TIMEZONE),
            ))
        except ValueError:
            continue

    # Expand recurring blocked slots into the search window
    for day_offset in range(8):
        d = (now + timedelta(days=day_offset)).date()
        for bs in prefs.get("blocked_slots", []):
            try:
                busy.append((
                    datetime.combine(d, time.fromisoformat(bs["start"]), tzinfo=TIMEZONE),
                    datetime.combine(d, time.fromisoformat(bs["end"]),   tzinfo=TIMEZONE),
                ))
            except (KeyError, ValueError):
                continue

    def is_slot_free(s: datetime) -> bool:
        e = s + timedelta(hours=SLOT_DURATION_HOURS)
        return all(e <= b_start or s >= b_end for b_start, b_end in busy)

    if is_slot_free(slot_start):
        return (
            f"Free. start_iso={slot_start.strftime('%Y-%m-%dT%H:%M:%S')} "
            f"end_iso={slot_end.strftime('%Y-%m-%dT%H:%M:%S')}"
        )

    # Find nearest free slot within the 7-day window
    candidate = datetime.combine(now.date(), day_start, tzinfo=TIMEZONE)
    while candidate + timedelta(hours=SLOT_DURATION_HOURS) <= window_end:
        c_end = candidate + timedelta(hours=SLOT_DURATION_HOURS)
        if c_end > now and candidate.time() >= day_start and c_end.time() <= day_end:
            if is_slot_free(candidate):
                return (
                    f"Busy. Nearest free slot: {candidate.strftime('%-d %b (%a) %-I:%M %p')} – "
                    f"{c_end.strftime('%-I:%M %p')} | "
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
    property_line = f"\nProperty: {VIEWING_PROPERTY_ADDRESS}" if VIEWING_PROPERTY_ADDRESS else ""

    event = {
        "summary": "Property Viewing",
        "description": f"Viewing arranged via Tenably\nDate/Time: {friendly}{property_line}\n\nPlease accept or decline this invite.",
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

    return "Invite sent to renter. Reply to the landlord with exactly: triple checking with renter"


def build_system_prompt() -> str:
    available_slots = get_free_slots()
    now_str = datetime.now(tz=TIMEZONE).strftime("%A %-d %B %Y, %-I:%M %p %Z")
    return f"""You are a WhatsApp assistant for Tenably arranging a property viewing on behalf of the renter.

Current date and time: {now_str}

Rules:
- Max 20 words per reply. Casual, warm, human tone. No paragraphs, no bullet lists.
- No emojis. Minimal punctuation. No commas unless absolutely necessary. Exclamation marks only occasionally at the end of a sentence.
- Only propose times from the available slots below. If the landlord suggests another time, call check_slot_availability first — if it's free confirm it, if not suggest the nearest free slot returned by the tool.
- When a time is agreed and confirmed free, call create_viewing_event, then reply to the landlord with exactly: triple checking with renter

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
            event = service.events().get(calendarId=CALENDAR_ID, eventId=event_id).execute()
            for attendee in event.get("attendees", []):
                if attendee["email"] == RENTER_EMAIL and attendee.get("responseStatus") == "accepted":
                    start_str = event["start"].get("dateTime", "")
                    if start_str:
                        start_dt = datetime.fromisoformat(start_str).astimezone(TIMEZONE)
                        friendly = start_dt.strftime("%-d %B at %-I:%M %p")
                    else:
                        friendly = "the agreed time"
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
    body = request.form.get("Body", "")
    sender = request.form.get("From", "")

    reply = handle_message(body, sender)

    response = MessagingResponse()
    response.message(reply)
    return str(response)


if __name__ == "__main__":
    app.run(debug=True, port=5000)
