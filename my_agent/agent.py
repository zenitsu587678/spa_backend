"""
Single-file ADK agent for the Luxury Spa Django backend.

This talks directly to the SAME database as your Django site, through the
Django ORM (bookings.models: Service, TimeSlot, Booking) -- not through a
separate SQLite file. Whatever spabackend/settings.py's DATABASES points at
(currently db.sqlite3) is what this agent reads and writes. If you ever move
the Django project to Postgres/MySQL, this file doesn't need to change.

Drop this in as spa_backend/my_agent/agent.py (replacing the stub that's
there now). The existing my_agent/__init__.py already does `from . import
agent`, and my_agent/.env already has GOOGLE_API_KEY set, so nothing else
needs to move.

Run from the project root (same folder as manage.py):
    adk run my_agent
    adk web --port 8001   # Django's runserver already owns 8000

NOTE ON ASYNC: `adk web` runs everything inside an asyncio event loop.
Django's ORM refuses to execute synchronous queries directly inside a
running event loop (raises SynchronousOnlyOperation) as a safety guard
against corrupting connections shared across threads. So every tool here is
`async def`, and the actual ORM work happens in an internal `_..._sync`
helper run through `sync_to_async`, which hands it off to a worker thread
where Django's normal synchronous ORM is safe to use. If you add a new
tool, follow the same pattern: write a plain sync `_thing_sync(...)`
function that touches the ORM, then a one-line `async def thing(...)`
wrapper that awaits `sync_to_async(_thing_sync)(...)`.

NOTE ON SCOPE: this combines what the original example split into a
customer-facing "booking agent" and a staff-facing "admin agent" into one
root_agent with all nine tools, since you asked for one file. That means
anyone talking to this agent can also call the staff tools (add_timeslot,
block_timeslot, list_bookings, get_waitlist) -- fine for local testing, but
before exposing this agent to real customers you'll want to either (a) split
it back into two Agent objects (two lists of tools, still fine in one file)
with the admin one gated behind auth, or (b) keep the admin tools but check
some caller identity before letting the model use them. Say the word and
I'll split it out.
"""

import os
import sys
import datetime
from typing import Optional


# --- Bootstrap Django so the ORM works outside of `manage.py runserver` ----
# my_agent/ sits next to spabackend/ (the settings package) and manage.py at
# the project root, so put the project root on sys.path and point Django at
# the same settings module the site itself uses.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "spabackend.settings")

import django  # noqa: E402
django.setup()

from asgiref.sync import sync_to_async  # noqa: E402
from google.genai import types  # noqa: E402
from google.adk.agents.llm_agent import Agent  # noqa: E402

from bookings.models import Booking, ContactMessage, Service, TimeSlot  # noqa: E402
from bookings import services  # noqa: E402

# Retry transient Gemini API errors (503 "high demand", 429 rate limits, etc.)
# with exponential backoff instead of failing the whole turn on one blip.
_RETRY_CONFIG = types.GenerateContentConfig(
    http_options=types.HttpOptions(
        retry_options=types.HttpRetryOptions(
            attempts=5,
            initial_delay=1,
            exp_base=2,
            max_delay=30,
            http_status_codes=[408, 429, 500, 502, 503, 504],
        ),
        timeout=60_000,
    ),
)


# --- small parsing helpers ---------------------------------------------------

def _parse_date(value: str):
    try:
        return datetime.datetime.strptime(value, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _parse_time(value: str):
    try:
        return datetime.datetime.strptime(value, "%H:%M").time()
    except (ValueError, TypeError):
        return None


# --- email ---------------------------------------------------------------
# Delegates to bookings.services.send_notification_email so chat-triggered
# and contact-form-triggered emails behave identically: dummy/console until
# EMAIL_HOST is set in settings.py, then real via django.core.mail.send_mail.

def _send_email(to_email: str, subject: str, body: str) -> None:
    services.send_notification_email(to_email, subject, body)


# --- customer-facing tools ---------------------------------------------------

def _get_service_info_sync(service_name: str = "") -> dict:
    qs = Service.objects.filter(is_active=True)
    if service_name:
        qs = qs.filter(name__icontains=service_name)

    services = [
        {
            "code": s.code,
            "name": s.name,
            "price": float(s.price),
            "duration_minutes": s.duration_minutes,
        }
        for s in qs
    ]
    if not services:
        return {"status": "not_found", "services": []}
    return {"status": "success", "services": services}


async def get_service_info(service_name: str = "") -> dict:
    """Looks up active spa services and their pricing/duration.

    Args:
        service_name: name (or partial name) of a service to look up.
            Leave empty to list all active services.

    Returns:
        dict with status and a list of matching services (code, name, price,
        duration_minutes).
    """
    return await sync_to_async(_get_service_info_sync, thread_sensitive=True)(service_name)


def _check_availability_sync(date: str, service_code: str = "") -> dict:
    booking_date = _parse_date(date)
    if booking_date is None:
        return {"status": "error", "message": "date must be in YYYY-MM-DD format."}

    if service_code and not Service.objects.filter(code=service_code, is_active=True).exists():
        return {"status": "service_not_found", "open_times": []}

    # Same helper the booking page's date picker calls, so the agent and
    # the website can never disagree about what's open on a given date.
    open_times = services.open_times_for_date(booking_date)

    return {"status": "success", "date": date, "open_times": open_times}


async def check_availability(date: str, service_code: str = "") -> dict:
    """Finds open appointment times on a given date.

    Time slots are shared across services (there's no separate calendar per
    service), so this returns every active time slot that doesn't already
    have a CONFIRMED booking on that date.

    Args:
        date: date to check, in YYYY-MM-DD format.
        service_code: optional service code to validate exists (from
            get_service_info) before checking times.

    Returns:
        dict with status and a list of open times (HH:MM).
    """
    return await sync_to_async(_check_availability_sync, thread_sensitive=True)(date, service_code)


def _book_slot_sync(service_code: str, date: str, time: str, full_name: str, email: str,
                     phone: str, therapist: str = "No preference", notes: str = "") -> dict:
    service = Service.objects.filter(code=service_code, is_active=True).first()
    if not service:
        return {"status": "error", "message": f"Unknown or inactive service code '{service_code}'."}

    booking_date = _parse_date(date)
    booking_time = _parse_time(time)
    if booking_date is None or booking_time is None:
        return {"status": "error", "message": "date must be YYYY-MM-DD and time must be HH:MM."}

    slot_taken = Booking.objects.filter(
        date=booking_date, time=booking_time, status=Booking.Status.CONFIRMED
    ).exists()
    status = Booking.Status.WAITLISTED if slot_taken else Booking.Status.CONFIRMED

    booking = Booking.objects.create(
        full_name=full_name,
        email=email,
        phone=phone,
        service=service,
        date=booking_date,
        time=booking_time,
        therapist=therapist,
        notes=notes,
        status=status,
        consent_given=True,
    )

    if slot_taken:
        _send_email(
            email,
            f"You're on the waitlist for {service.name}",
            f"Hi {full_name},\n\nThat slot is full, so we've added you to the waitlist for "
            f"{service.name} on {date} at {time}. We'll email you if it opens up.\n\n"
            f"Reference: {booking.booking_id}",
        )
        return {
            "status": "waitlisted",
            "booking_id": booking.booking_id,
            "message": f"{full_name} has been waitlisted for {service.name} on {date} at {time}.",
        }

    _send_email(
        email,
        f"Your {service.name} appointment is confirmed",
        f"Hi {full_name},\n\nYour {service.name} appointment is confirmed for {date} at {time}.\n\n"
        f"Reference: {booking.booking_id}",
    )
    return {
        "status": "confirmed",
        "booking_id": booking.booking_id,
        "message": (
            f"Booking confirmed for {full_name} -- {service.name} on {date} at {time}. "
            f"Reference: {booking.booking_id}."
        ),
    }


async def book_slot(service_code: str, date: str, time: str, full_name: str, email: str,
                     phone: str, therapist: str = "No preference", notes: str = "") -> dict:
    """Books an appointment. If the date/time is already confirmed for someone
    else, the customer is automatically added to the waitlist instead.

    Args:
        service_code: service code from get_service_info (e.g. "swedish").
        date: appointment date, YYYY-MM-DD.
        time: appointment time, HH:MM (24h).
        full_name: customer's full name.
        email: customer's email, used for confirmation and as their lookup
            reference along with the booking id.
        phone: customer's phone number.
        therapist: preferred therapist name, or "No preference".
        notes: any extra notes from the customer.

    Returns:
        dict with status ('confirmed' or 'waitlisted') and a booking_id.
    """
    return await sync_to_async(_book_slot_sync, thread_sensitive=True)(
        service_code, date, time, full_name, email, phone, therapist, notes
    )


def _cancel_booking_sync(booking_id: str) -> dict:
    booking = Booking.objects.filter(booking_id=booking_id).first()
    if not booking:
        return {"status": "error", "message": "No booking found with that reference."}
    if booking.status == Booking.Status.CANCELLED:
        return {"status": "already_cancelled", "message": "This booking is already cancelled."}

    was_confirmed = booking.status == Booking.Status.CONFIRMED
    booking.status = Booking.Status.CANCELLED
    booking.save(update_fields=["status", "updated_at"])

    promoted_note = ""
    if was_confirmed:
        next_waitlisted = (
            Booking.objects.filter(date=booking.date, time=booking.time,
                                    status=Booking.Status.WAITLISTED)
            .order_by("created_at")
            .first()
        )
        if next_waitlisted:
            next_waitlisted.status = Booking.Status.CONFIRMED
            next_waitlisted.save(update_fields=["status", "updated_at"])
            _send_email(
                next_waitlisted.email,
                f"A spot opened up for your {next_waitlisted.service_name} appointment!",
                f"Hi {next_waitlisted.full_name},\n\nGood news -- your {next_waitlisted.service_name} "
                f"appointment is now confirmed for {next_waitlisted.date} at {next_waitlisted.time}.\n\n"
                f"Reference: {next_waitlisted.booking_id}",
            )
            promoted_note = f" {next_waitlisted.full_name} was promoted from the waitlist into that slot."

    return {"status": "cancelled", "message": f"Booking {booking_id} cancelled.{promoted_note}"}


async def cancel_booking(booking_id: str) -> dict:
    """Cancels a booking by its reference (e.g. SPA-2026-1234). If it was
    confirmed and freed up a slot, the earliest waitlisted booking for that
    same date/time is automatically promoted and emailed.

    Args:
        booking_id: the booking reference given at booking time.

    Returns:
        dict with the cancellation status.
    """
    return await sync_to_async(_cancel_booking_sync, thread_sensitive=True)(booking_id)


def _get_confirmed_booking_sync(booking_id: str):
    return Booking.objects.filter(booking_id=booking_id, status=Booking.Status.CONFIRMED).first()


async def reschedule_booking(booking_id: str, new_date: str, new_time: str) -> dict:
    """Reschedules an existing confirmed booking to a new date/time. Frees the
    old slot (promoting a waitlisted customer into it, if any) and re-books
    under a new booking_id.

    Args:
        booking_id: the existing booking's reference.
        new_date: new date, YYYY-MM-DD.
        new_time: new time, HH:MM.

    Returns:
        dict with the status of the reschedule and the new booking_id.
    """
    booking = await sync_to_async(_get_confirmed_booking_sync, thread_sensitive=True)(booking_id)
    if not booking:
        return {"status": "error", "message": "No active confirmed booking found with that reference."}

    cancel_result = await cancel_booking(booking_id)
    if cancel_result["status"] != "cancelled":
        return cancel_result

    return await book_slot(
        booking.service.code, new_date, new_time,
        booking.full_name, booking.email, booking.phone,
        booking.therapist, booking.notes,
    )


# --- staff-facing tools -------------------------------------------------------

def _add_timeslot_sync(time: str) -> dict:
    parsed = _parse_time(time)
    if parsed is None:
        return {"status": "error", "message": "time must be HH:MM."}

    slot, created = TimeSlot.objects.get_or_create(time=parsed, defaults={"is_active": True})
    if not created and not slot.is_active:
        slot.is_active = True
        slot.save(update_fields=["is_active"])
    return {"status": "success", "message": f"Time slot {time} is now open for booking every day."}


async def add_timeslot(time: str) -> dict:
    """Opens up a bookable time slot (applies every day -- the schedule here
    isn't per-date, it's a recurring daily template, matching how TimeSlot
    works in bookings/models.py).

    Args:
        time: time to open, HH:MM (24h).

    Returns:
        dict with status of the change.
    """
    return await sync_to_async(_add_timeslot_sync, thread_sensitive=True)(time)


def _block_timeslot_sync(time: str) -> dict:
    parsed = _parse_time(time)
    if parsed is None:
        return {"status": "error", "message": "time must be HH:MM."}

    slot = TimeSlot.objects.filter(time=parsed).first()
    if not slot:
        return {"status": "error", "message": f"No time slot exists at {time}."}

    slot.is_active = False
    slot.save(update_fields=["is_active"])
    return {
        "status": "success",
        "message": (
            f"Time slot {time} is now closed for new bookings, every day (this schedule "
            f"isn't per-date). Existing bookings at that time are unaffected."
        ),
    }


async def block_timeslot(time: str) -> dict:
    """Closes a recurring daily time slot so it can no longer be booked.
    Existing bookings already made at that time are untouched.

    Args:
        time: time to close, HH:MM (24h).

    Returns:
        dict with status of the change.
    """
    return await sync_to_async(_block_timeslot_sync, thread_sensitive=True)(time)


def _list_bookings_sync(date: str = "", status: str = "") -> dict:
    qs = Booking.objects.all()
    if date:
        parsed = _parse_date(date)
        if parsed is None:
            return {"status": "error", "message": "date must be YYYY-MM-DD."}
        qs = qs.filter(date=parsed)
    if status:
        qs = qs.filter(status=status.upper())

    bookings = [
        {
            "booking_id": b.booking_id,
            "customer": b.full_name,
            "email": b.email,
            "service": b.service_name,
            "date": b.date.isoformat(),
            "time": b.time.strftime("%H:%M"),
            "status": b.status,
        }
        for b in qs.order_by("date", "time")
    ]
    return {"status": "success", "bookings": bookings}


async def list_bookings(date: str = "", status: str = "") -> dict:
    """Lists bookings, optionally filtered by date and/or status.

    Args:
        date: optional YYYY-MM-DD filter.
        status: optional filter -- one of PENDING, CONFIRMED, COMPLETED,
            CANCELLED, NO_SHOW, WAITLISTED.

    Returns:
        dict with status and a list of matching bookings.
    """
    return await sync_to_async(_list_bookings_sync, thread_sensitive=True)(date, status)


def _get_waitlist_sync(date: str = "") -> dict:
    qs = Booking.objects.filter(status=Booking.Status.WAITLISTED)
    if date:
        parsed = _parse_date(date)
        if parsed is None:
            return {"status": "error", "message": "date must be YYYY-MM-DD."}
        qs = qs.filter(date=parsed)

    waitlist = [
        {
            "booking_id": b.booking_id,
            "customer": b.full_name,
            "email": b.email,
            "service": b.service_name,
            "date": b.date.isoformat(),
            "time": b.time.strftime("%H:%M"),
            "requested_at": b.created_at.isoformat(),
        }
        for b in qs.order_by("created_at")
    ]
    return {"status": "success", "waitlist": waitlist}


async def get_waitlist(date: str = "") -> dict:
    """Lists customers currently on the waitlist, oldest request first.

    Args:
        date: optional YYYY-MM-DD filter.

    Returns:
        dict with status and the waitlist entries.
    """
    return await sync_to_async(_get_waitlist_sync, thread_sensitive=True)(date)

def _process_rescheduling_sync(message_id: int, requested_date: str = "", requested_time: str = "") -> dict:
    try:
        msg = ContactMessage.objects.get(id=message_id)
    except ContactMessage.DoesNotExist:
        return {"status": "error",
                "message": "The provided message_id does not exist in the database."}

    # services.process_reschedule looks the booking up by msg.name/msg.email
    # (both must match, same rule the contact-form flow uses), checks the
    # real calendar (TimeSlot + any conflicting CONFIRMED booking), and
    # either moves the booking or waitlists it -- see bookings/services.py.
    # Either requested_date or requested_time can be left blank: whichever
    # one is blank keeps the booking's current value (e.g. a time-only
    # change like "move my 11am to 1pm" doesn't need a date at all).
    return services.process_reschedule(msg, requested_date or None, requested_time or None)


async def process_rescheduling_tool(message_id: int, requested_date: str = "", requested_time: str = "") -> dict:
    """Processes a customer's request to reschedule their spa booking --
    the date, the time, or both.

    Call this ONLY when a customer explicitly asks to change/reschedule an
    existing appointment and you have at least a new date OR a new time for
    them (ask the customer for one first if their message gives neither).

    The customer is matched to their existing booking using the name and
    email already on the ContactMessage record (both must match) -- so this
    only needs the message id and whatever new date/time they gave, not the
    customer's details again.

    Args:
        message_id: The database ID of the ContactMessage this request came
            from.
        requested_date: The new date, in YYYY-MM-DD format, or "" if the
            customer only asked to change the time (the booking's current
            date is kept). Convert whatever they wrote (e.g. "25 August")
            into that format before calling this tool.
        requested_time: The new time, in 24-hour HH:MM format, or "" if the
            customer only asked to change the date (the booking's current
            time is kept). Convert whatever they wrote (e.g. "1pm") before
            calling this tool. If they mention two times ("from 11am to
            1pm"), pass the SECOND one -- that's the one they want.

    Returns:
        dict with status ('rescheduled', 'waitlisted', 'no_booking_found',
        'needs_clarification', or 'error') and a customer-facing message.
    """
    return await sync_to_async(_process_rescheduling_sync, thread_sensitive=True)(
        message_id, requested_date, requested_time
    )


# --- checking the contact-form pipeline from chat ----------------------------
# These two tools exist so you can verify the "Contact us" -> agent pipeline
# is actually working by just chatting: ask "any new contact messages?" and
# the agent calls list_contact_messages and shows you what came in; ask it
# to "process message 4" and it calls process_contact_message, which runs
# the exact same classify-then-act logic the contact form itself triggers
# automatically, and its reply shows up right here in the chat.

def _list_contact_messages_sync(status: str = "") -> dict:
    qs = ContactMessage.objects.all()
    if status:
        qs = qs.filter(status=status.upper())

    messages = [
        {
            "id": m.id,
            "name": m.name,
            "email": m.email,
            "message": m.message,
            "status": m.status,
            "intent": m.intent,
            "agent_reply": m.agent_reply,
            "created_at": m.created_at.isoformat(),
        }
        for m in qs.order_by("-created_at")[:20]
    ]
    return {"status": "success", "messages": messages}


async def list_contact_messages(status: str = "") -> dict:
    """Lists the 20 most recent messages submitted through the website's
    Contact us form (the ContactMessage table -- the "2nd database").

    Args:
        status: optional filter -- one of PENDING, PROCESSING, PROCESSED,
            WAITING_FOR_CUSTOMER, FAILED. Leave empty to see all.

    Returns:
        dict with status and a list of messages, including whatever reply
        the agent already gave (if any) in `agent_reply`.
    """
    return await sync_to_async(_list_contact_messages_sync, thread_sensitive=True)(status)


def _process_contact_message_sync(message_id: int) -> dict:
    try:
        msg = ContactMessage.objects.get(id=message_id)
    except ContactMessage.DoesNotExist:
        return {"status": "error", "message": "No contact message with that id."}
    return services.handle_contact_message(msg)


async def process_contact_message(message_id: int) -> dict:
    """Runs the agent's classify-then-act logic on one Contact-us message --
    the same thing that happens automatically the instant the form is
    submitted. Useful for re-running a message manually, or just for
    checking the pipeline actually works by watching the reply show up
    here in the chat.

    Args:
        message_id: The database ID of the ContactMessage to process.

    Returns:
        dict with the outcome status and the customer-facing reply.
    """
    return await sync_to_async(_process_contact_message_sync, thread_sensitive=True)(message_id)

# --- agent ---------------------------------------------------------------

root_agent = Agent(
    # Pinned to a specific stable version rather than "-latest", which can
    # route to newer/higher-demand endpoints more prone to 503s.
    model="gemini-3.6-flash",
    generate_content_config=_RETRY_CONFIG,
    name="root_agent",
    description=(
        "Assistant for the Luxury Spa: helps customers check services and "
        "availability, book/waitlist/cancel/reschedule appointments, and "
        "helps staff manage the schedule and see bookings/waitlist."
    ),
    instruction=(
        "You help with a spa's bookings. You serve two kinds of users -- "
        "customers -- so pay attention to which one you're talking to.\n\n"
        "For customers:\n"
        "1. Answer questions about services and pricing with get_service_info.\n"
        "2. To book, use check_availability to find open times on the date they "
        "want, confirm the details with them, then call book_slot.\n"
        "3. If book_slot returns status 'waitlisted', tell them clearly they've "
        "been waitlisted, not confirmed, and that they'll be emailed if a spot opens.\n"
        "4. For cancelling or rescheduling when you're chatting with the "
        "customer directly and they have their booking reference (e.g. "
        "SPA-2026-1234), use cancel_booking or reschedule_booking. If instead "
        "you're processing a submitted Contact-us message (you'll be given a "
        "message_id) that asks to change a booking date, use "
        "process_rescheduling_tool with that message_id and the requested "
        "date in YYYY-MM-DD format.\n\n"
        "For staff:\n"
        "5. Use add_timeslot / block_timeslot to open or close recurring daily "
        "time slots, list_bookings to look up the schedule, and get_waitlist to "
        "see who's waiting. Always confirm the exact time/date/status back "
        "before making a change, since it affects what customers can book.\n"
        "6. Use list_contact_messages to see what's come in through the "
        "website's Contact us form, and process_contact_message to (re)run "
        "the agent on one of them -- useful for checking the pipeline is "
        "working, or handling a message that needed clarification.\n\n"
        "General:\n"
        "7. Never invent service codes, prices, times, or booking references -- "
        "always get them from a tool call.\n"
        "8. Data changes outside this conversation all the time (staff editing "
        "things in Django admin, other customers booking slots). Never answer "
        "from a tool result you already have earlier in this chat -- call the "
        "relevant tool again every time you're asked about current availability, "
        "bookings, waitlist, or service info, even if it looks unchanged.\n"
        "9. Be concise and warm. Confirm key details (service, date, time, "
        "price, reference) back to whoever you're helping."
    ),
    tools=[
        get_service_info,
        check_availability,
        book_slot,
        cancel_booking,
        reschedule_booking,
        block_timeslot,
        list_bookings,
        get_waitlist,
        process_rescheduling_tool,
        list_contact_messages,
        process_contact_message,
    ],
)
