"""
Shared logic for the "Contact us" -> agent -> booking-database flow.

Why this file exists: the reschedule logic needs to run from two different
places -- (1) synchronously, right inside the Django view that handles the
contact form POST, and (2) as an async ADK tool when someone chats with
my_agent directly. Keeping ONE plain-sync implementation here and having
both call sites wrap it appropriately avoids the bugs the previous version
had (wrong field names, a fake availability check, and an ORM call made
from inside an async context).

Flow used by bookings.views.submit_contact:
    1. save the ContactMessage (the "2nd database" row)
    2. classify_contact_message() asks Gemini (or falls back to keyword
       matching if no API key / the call fails) what the customer wants
    3. if it's a reschedule request with a usable date, process_reschedule()
       looks up the booking by name+email, checks the real calendar, and
       either moves the booking or waitlists it
    4. a confirmation "email" is printed to the console (send_dummy_email) --
       swap that body for django.core.mail.send_mail once SMTP is configured
"""

import datetime
import json
import os
import re
from pathlib import Path
from typing import Optional

from .models import Booking, ContactMessage, TimeSlot

BASE_DIR = Path(__file__).resolve().parent.parent

DATE_FORMATS_WITH_YEAR = (
    "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%Y/%m/%d",
    "%d %B %Y", "%d %b %Y", "%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%b %d %Y",
)
DATE_FORMATS_NO_YEAR = ("%d %B", "%d %b", "%B %d", "%b %d")

RESCHEDULE_KEYWORDS = (
    "reschedule", "change my booking", "change the date", "change my date",
    "change my appointment", "move my appointment", "move my booking",
    "different date", "another date", "new date", "postpone",
    "can't make it", "cant make it", "cannot make it", "won't be able",
    "wont be able", "not be able to make", "not able to make",
)


# --- email ---------------------------------------------------------------
# Sends for real once EMAIL_HOST is set in settings.py / your .env; until
# then it prints to the console and saves a copy on the ContactMessage (if
# one is passed in) so you can see it in the admin without a terminal open.

def send_notification_email(to_email: str, subject: str, body: str,
                             contact_message: Optional[ContactMessage] = None) -> None:
    from django.conf import settings

    email_host = getattr(settings, "EMAIL_HOST", "") or ""
    if email_host.strip():
        from django.core.mail import send_mail
        send_mail(
            subject, body,
            getattr(settings, "DEFAULT_FROM_EMAIL", "no-reply@luxuryspa.example"),
            [to_email], fail_silently=False,
        )
    else:
        print("=" * 60)
        print("DUMMY EMAIL (EMAIL_HOST not configured -- not actually sent)")
        print(f"To: {to_email}")
        print(f"Subject: {subject}\n")
        print(body)
        print("=" * 60)

    if contact_message is not None:
        contact_message.email_preview = f"To: {to_email}\nSubject: {subject}\n\n{body}"
        contact_message.save(update_fields=["email_preview"])


def send_dummy_email(to_email: str, subject: str, body: str) -> None:
    """Back-compat alias -- see send_notification_email."""
    send_notification_email(to_email, subject, body)


# --- date parsing -------------------------------------------------------------

def parse_date_flexible(value: str, today: Optional[datetime.date] = None) -> Optional[datetime.date]:
    """Accepts ISO dates as well as looser forms like '25 August' or
    'August 25'. Formats with no year assume the current year, rolling to
    next year if that date has already passed."""
    if not value:
        return None
    value = value.strip()
    today = today or datetime.date.today()

    for fmt in DATE_FORMATS_WITH_YEAR:
        try:
            return datetime.datetime.strptime(value, fmt).date()
        except ValueError:
            continue

    for fmt in DATE_FORMATS_NO_YEAR:
        try:
            parsed = datetime.datetime.strptime(value, fmt).replace(year=today.year).date()
            if parsed < today:
                parsed = parsed.replace(year=today.year + 1)
            return parsed
        except ValueError:
            continue

    return None


_MONTHS = ("January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December")


def extract_date_from_text(text: str) -> Optional[str]:
    """Best-effort scan of free text for a date, used by the keyword-based
    fallback classifier when the Gemini call isn't available. Returns an
    ISO date string or None."""
    iso_match = re.search(r"\b\d{4}-\d{2}-\d{2}\b", text)
    if iso_match:
        parsed = parse_date_flexible(iso_match.group(0))
        return parsed.isoformat() if parsed else None

    month_names = "|".join(_MONTHS)
    patterns = [
        rf"\b(\d{{1,2}})\s+({month_names})(?:\s+(\d{{4}}))?\b",
        rf"\b({month_names})\s+(\d{{1,2}})(?:,?\s+(\d{{4}}))?\b",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if not m:
            continue
        groups = [g for g in m.groups() if g]
        candidate = " ".join(groups[:2]) if not groups[0].isdigit() else None
        # normalise both orderings into "<day> <month> [<year>]"
        raw = m.group(0)
        parsed = parse_date_flexible(raw.replace(",", ""))
        if parsed:
            return parsed.isoformat()
    return None


# --- time parsing ------------------------------------------------------------

TIME_FORMATS = ("%H:%M", "%I:%M%p", "%I:%M %p", "%I%p", "%I %p")


def parse_time_flexible(value: str) -> Optional[datetime.time]:
    """Accepts 24-hour 'HH:MM' as well as '1pm', '1:30pm', '1:30 PM', etc."""
    if not value:
        return None
    value = value.strip().upper().replace(".", "")
    for fmt in TIME_FORMATS:
        try:
            return datetime.datetime.strptime(value, fmt).time()
        except ValueError:
            continue
    return None


def extract_time_from_text(text: str) -> Optional[str]:
    """Best-effort scan of free text for the TARGET time the customer wants.
    Messages like 'reschedule from 11am to 1pm' mention two times -- the
    last one mentioned is treated as the target, which matches how people
    normally phrase this ('move it from X to Y', 'change my 11am to 1pm').
    Returns 'HH:MM' (24-hour) or None."""
    pattern = r"\b(\d{1,2})(?::(\d{2}))?\s*([APap][Mm])\b"
    matches = list(re.finditer(pattern, text))
    if not matches:
        # fall back to bare 24-hour times, e.g. "13:00" -- but only when
        # not immediately adjacent to '-' or '/' so we don't grab a date.
        for m in re.finditer(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", text):
            start, end = m.span()
            if start > 0 and text[start - 1] in "-/":
                continue
            if end < len(text) and text[end] in "-/":
                continue
            matches.append(m)
        if not matches:
            return None

    last = matches[-1]
    raw = last.group(0).replace(" ", "")
    parsed = parse_time_flexible(raw)
    return parsed.strftime("%H:%M") if parsed else None


# --- intent detection -----------------------------------------------------

def _get_google_api_key() -> str:
    """settings.py already loads the project-root .env into os.environ; this
    also falls back to my_agent/.env (used by the ADK chat agent) so the
    contact-form flow works even if only that file has the key."""
    key = os.environ.get("GOOGLE_API_KEY", "").strip()
    if key:
        return key

    agent_env = BASE_DIR / "my_agent" / ".env"
    if agent_env.exists():
        for line in agent_env.read_text().splitlines():
            line = line.strip()
            if line.startswith("GOOGLE_API_KEY="):
                key = line.split("=", 1)[1].strip().strip('"').strip("'")
                os.environ.setdefault("GOOGLE_API_KEY", key)
                return key
    return ""


def _heuristic_classify(message_text: str) -> dict:
    lowered = message_text.lower()
    if any(kw in lowered for kw in RESCHEDULE_KEYWORDS):
        return {
            "intent": "RESCHEDULE_BOOKING",
            "requested_date": extract_date_from_text(message_text),
            "requested_time": extract_time_from_text(message_text),
        }
    return {"intent": "GENERAL_INQUIRY", "requested_date": None, "requested_time": None}


def classify_contact_message(message_text: str) -> dict:
    """Returns {"intent": "RESCHEDULE_BOOKING" | "GENERAL_INQUIRY",
    "requested_date": "YYYY-MM-DD" | None, "requested_time": "HH:MM" | None}.
    Either of requested_date/requested_time can be None -- a customer might
    only be changing the time (same day) or only the date (same time).

    Tries Gemini first (so it understands phrasing the keyword list would
    miss, e.g. "I won't be able to make it, could we push it back?");
    falls back to keyword + regex matching if no API key is configured or
    the call fails for any reason, so the contact form still works without
    the AI dependency wired up.
    """
    api_key = _get_google_api_key()
    if api_key:
        try:
            from google import genai
            from google.genai import types

            client = genai.Client(api_key=api_key)
            prompt = (
                "You classify messages sent through a spa's 'Contact us' form.\n"
                "Decide if the customer is asking to reschedule/change the date "
                "and/or time of an EXISTING booking, or something else (a "
                "general inquiry).\n"
                "A reschedule request might change only the date (same time), "
                "only the time (same date), or both. For example 'can I move my "
                "11am to 1pm on the same day' is a TIME-only change -- do not "
                "invent a date for it.\n"
                "If a new date is mentioned, extract it as ISO (YYYY-MM-DD). "
                "Today's date is "
                f"{datetime.date.today().isoformat()}. If no year is given, "
                "assume the nearest future occurrence.\n"
                "If a new time is mentioned, extract it as 24-hour HH:MM. When "
                "the message names two times (e.g. 'from 11am to 1pm'), the "
                "SECOND one is the target time they want -- extract only that "
                "one, not the original.\n\n"
                f"Message:\n{message_text}\n\n"
                'Respond with ONLY this JSON shape, no other text: '
                '{"intent": "RESCHEDULE_BOOKING" or "GENERAL_INQUIRY", '
                '"requested_date": "YYYY-MM-DD" or null, '
                '"requested_time": "HH:MM" or null}'
            )
            response = client.models.generate_content(
                model="gemini-3.6-flash",
                contents=prompt,
                config=types.GenerateContentConfig(response_mime_type="application/json"),
            )
            data = json.loads(response.text)
            intent = data.get("intent") if data.get("intent") in (
                "RESCHEDULE_BOOKING", "GENERAL_INQUIRY") else "GENERAL_INQUIRY"
            requested_date = data.get("requested_date") or None
            if requested_date and parse_date_flexible(requested_date) is None:
                requested_date = extract_date_from_text(message_text)
            requested_time = data.get("requested_time") or None
            if requested_time and parse_time_flexible(requested_time) is None:
                requested_time = extract_time_from_text(message_text)
            return {"intent": intent, "requested_date": requested_date,
                    "requested_time": requested_time}
        except Exception as exc:  # network error, bad key, bad response shape, etc.
            print(f"[services.classify_contact_message] Gemini call failed, "
                  f"falling back to keyword matching: {exc}")

    return _heuristic_classify(message_text)


# --- calendar / booking lookup ----------------------------------------------

def slot_is_available(date_obj: datetime.date, time_obj: datetime.time,
                       exclude_booking_id: Optional[int] = None) -> bool:
    """True if this date+time has no conflicting CONFIRMED booking, and (when
    TimeSlots are configured at all) the time is one of the open slots."""
    if TimeSlot.objects.filter(is_active=True).exists() and \
            not TimeSlot.objects.filter(time=time_obj, is_active=True).exists():
        return False

    conflicts = Booking.objects.filter(
        date=date_obj, time=time_obj, status=Booking.Status.CONFIRMED,
    )
    if exclude_booking_id:
        conflicts = conflicts.exclude(id=exclude_booking_id)
    return not conflicts.exists()


def open_times_for_date(date_obj: datetime.date) -> list:
    """All active time slots that do NOT already have a CONFIRMED booking on
    this specific date. This is per-date only -- a time slot taken on one
    day is still open on every other day. Returns a list of "HH:MM" strings,
    in order. Used by both the booking page (so a taken slot disappears from
    the dropdown just for that date) and the agent's check_availability
    tool, so the two never disagree."""
    all_times = list(TimeSlot.objects.filter(is_active=True).order_by("time")
                      .values_list("time", flat=True))
    taken_times = set(
        Booking.objects.filter(date=date_obj, status=Booking.Status.CONFIRMED)
        .values_list("time", flat=True)
    )
    return [t.strftime("%H:%M") for t in all_times if t not in taken_times]


def find_customer_booking(full_name: str, email: str) -> Optional[Booking]:
    """Finds the customer's booking by name AND email (both must match,
    case-insensitively). Prefers an upcoming CONFIRMED booking; falls back
    to the most recent non-cancelled booking if none is upcoming."""
    qs = Booking.objects.filter(
        full_name__iexact=full_name.strip(), email__iexact=email.strip(),
    ).exclude(status=Booking.Status.CANCELLED)

    upcoming_confirmed = qs.filter(
        status=Booking.Status.CONFIRMED, date__gte=datetime.date.today(),
    ).order_by("date", "time").first()
    if upcoming_confirmed:
        return upcoming_confirmed

    return qs.order_by("-created_at").first()


# --- the core reschedule action ----------------------------------------------

def process_reschedule(contact_message: ContactMessage, requested_date_raw: Optional[str] = None,
                        requested_time_raw: Optional[str] = None) -> dict:
    """Given a ContactMessage and whatever new date/time the customer asked
    for (either one, or both), finds their booking, checks the real
    calendar, and either moves the booking to that date/time (status stays
    CONFIRMED) or moves it there as WAITLISTED (same status Booking already
    uses for a fully-booked slot -- no separate waitlist table needed).
    Whichever of date/time wasn't mentioned keeps its current value from the
    existing booking. Mutates and saves `contact_message`, including
    `agent_reply` so the outcome is visible in the admin."""
    contact_message.status = "PROCESSING"
    contact_message.intent = "RESCHEDULE_BOOKING"
    contact_message.save(update_fields=["status", "intent"])

    if not requested_date_raw and not requested_time_raw:
        reply = (
            "It sounds like you'd like to change your appointment -- could "
            "you let us know the new date and/or time you'd like instead?"
        )
        contact_message.status = "WAITING_FOR_CUSTOMER"
        contact_message.agent_reply = reply
        contact_message.save(update_fields=["status", "agent_reply"])
        return {"status": "needs_clarification", "message": reply}

    parsed_date = parse_date_flexible(requested_date_raw) if requested_date_raw else None
    if requested_date_raw and parsed_date is None:
        reply = (
            "I couldn't tell what date you'd like to move to -- could you "
            "reply with a specific date, e.g. 2026-08-25?"
        )
        contact_message.status = "WAITING_FOR_CUSTOMER"
        contact_message.agent_reply = reply
        contact_message.save(update_fields=["status", "agent_reply"])
        return {"status": "needs_clarification", "message": reply}

    parsed_time = parse_time_flexible(requested_time_raw) if requested_time_raw else None
    if requested_time_raw and parsed_time is None:
        reply = (
            "I couldn't tell what time you'd like instead -- could you "
            "reply with a specific time, e.g. 1:00 PM?"
        )
        contact_message.status = "WAITING_FOR_CUSTOMER"
        contact_message.agent_reply = reply
        contact_message.save(update_fields=["status", "agent_reply"])
        return {"status": "needs_clarification", "message": reply}

    booking = find_customer_booking(contact_message.name, contact_message.email)
    if booking is None:
        reply = (
            f"I couldn't find an existing booking under the name "
            f"'{contact_message.name}' and email '{contact_message.email}'. "
            "Please double-check those match your original booking, or "
            "include your booking reference."
        )
        contact_message.status = "FAILED"
        contact_message.agent_reply = reply
        contact_message.save(update_fields=["status", "agent_reply"])
        return {"status": "no_booking_found", "message": reply}

    # Whichever of date/time the customer didn't mention keeps its current
    # value -- e.g. "change my time to 1pm" only touches the time, the date
    # stays what it already was on the booking.
    new_date = parsed_date or booking.date
    new_time = parsed_time or booking.time

    contact_message.related_booking = booking
    contact_message.requested_date = new_date
    contact_message.save(update_fields=["related_booking", "requested_date"])

    if slot_is_available(new_date, new_time, exclude_booking_id=booking.id):
        old_date, old_time = booking.date, booking.time
        booking.date = new_date
        booking.time = new_time
        booking.status = Booking.Status.CONFIRMED
        booking.save(update_fields=["date", "time", "status", "updated_at"])

        reply = (
            f"Done -- your {booking.service_name} appointment is now on "
            f"{new_date:%d %b %Y} at {new_time:%H:%M}. "
            f"Reference: {booking.booking_id}."
        )
        contact_message.status = "PROCESSED"
        contact_message.agent_reply = reply
        contact_message.save(update_fields=["status", "agent_reply"])

        send_notification_email(
            booking.email,
            "Your Luxury Spa appointment has been moved",
            f"Hi {booking.full_name},\n\nYour {booking.service_name} appointment "
            f"has been moved from {old_date:%d %b %Y} {old_time:%H:%M} to "
            f"{new_date:%d %b %Y} {new_time:%H:%M}.\n\nReference: {booking.booking_id}",
            contact_message=contact_message,
        )
        return {"status": "rescheduled", "message": reply, "booking_id": booking.booking_id}

    # Slot's full -> move the booking itself onto the waitlist at the
    # requested date/time, same as a brand-new booking would be.
    booking.date = new_date
    booking.time = new_time
    booking.status = Booking.Status.WAITLISTED
    booking.save(update_fields=["date", "time", "status", "updated_at"])

    reply = (
        f"{new_date:%d %b %Y} at {new_time:%H:%M} is fully booked, so "
        f"I've put you on the waitlist for that slot. We'll email you if "
        f"it opens up. Reference: {booking.booking_id}."
    )
    contact_message.status = "PROCESSED"
    contact_message.agent_reply = reply
    contact_message.save(update_fields=["status", "agent_reply"])

    send_notification_email(
        booking.email,
        "You're on the waitlist for a new slot",
        f"Hi {booking.full_name},\n\n{new_date:%d %b %Y} at {new_time:%H:%M} "
        f"is fully booked, so we've added you to the waitlist for that slot. "
        f"We'll email you if it opens up.\n\nReference: {booking.booking_id}",
        contact_message=contact_message,
    )
    return {"status": "waitlisted", "message": reply, "booking_id": booking.booking_id}


def handle_contact_message(contact_message: ContactMessage) -> dict:
    """Entry point called right after a ContactMessage is saved: classifies
    intent, then acts on it. Returns a dict with at least a "message" key
    suitable for showing back to the customer on the contact page."""
    classification = classify_contact_message(contact_message.message)
    intent = classification["intent"]

    if intent == "RESCHEDULE_BOOKING":
        return process_reschedule(
            contact_message,
            classification.get("requested_date"),
            classification.get("requested_time"),
        )

    reply = "Thanks for reaching out -- our team will get back to you shortly."
    contact_message.status = "PENDING"
    contact_message.intent = "GENERAL_INQUIRY"
    contact_message.agent_reply = reply
    contact_message.save(update_fields=["status", "intent", "agent_reply"])
    return {"status": "received", "message": reply}
