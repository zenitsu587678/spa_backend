# Luxury Spa — Django Booking Backend

## Where it started

This project began as a single Python script: a `BookingDatabase` class
doing raw SQL against a `sqlite3` file, and an `AgentAPIRequestHandler`
built on `http.server` handling incoming bookings. It worked, but it had no
owner-facing view at all — there was no way to see what had been booked
except by querying the database directly — and its `submit_to_website()`
function swallowed every error and returned success regardless, so a
booking could silently fail to save while the customer was told it went
through. It also had a Cloudflare Turnstile secret hardcoded directly in
the script.

## The move to Django

The script was rebuilt on Django, which brought three things the original
was missing at once: `bookings/models.py` replaced the raw SQL with an
ORM (so "find today's confirmed bookings" is a method call, not
hand-written SQL), `bookings/views.py` replaced the bespoke HTTP handler
with real Django views, and — the piece that had been missing
entirely — `/admin/` gave the spa owner an actual screen to see, filter,
search, and update bookings without touching the database file. The
`/widget.js` endpoint kept the same public behaviour the old script had,
so the booking form on the live site didn't need to change.

This is also where the error-swallowing bug got fixed: bookings now either
save to the database or return a real error, so what a customer sees
matches what shows up in `/admin/`. And the hardcoded Turnstile secret was
flagged as compromised, since it had been shared in plain text in the old
script — a fresh one needs generating in the Cloudflare dashboard before
this goes anywhere public.

## Adding a conversational agent

Once the booking data lived in a proper database with a real ORM, the
natural next step was letting people ask for things in plain English —
"what's open Thursday", "book Jane in for a facial" — instead of only
through the web form or admin clicks. That's `my_agent/`, built on
Google's Agent Development Kit (ADK) and Gemini.

The agent doesn't keep its own database. Every tool it has —
`get_service_info`, `check_availability`, `book_slot`, `cancel_booking`,
`reschedule_booking`, `block_timeslot`, `list_bookings`, `get_waitlist` —
reads and writes through the same Django ORM the website itself uses,
against the same `db.sqlite3`. A booking made by the agent shows up in
`/admin/` immediately, and a booking made through the web form is
immediately visible to the agent — there was never a second source of
truth to keep in sync.

## What it took to get the agent actually working

A few real problems showed up once the agent was running against live
Django models, each of which shaped how `my_agent/agent.py` ended up
looking:

- **Django and asyncio don't mix by default.** `adk web` runs inside an
  event loop, and Django refuses to run ordinary synchronous ORM queries
  directly on that loop — it raises `SynchronousOnlyOperation` rather than
  risk corrupting a shared database connection. Every tool ended up split
  into a private synchronous function that does the actual query, and a
  thin `async` wrapper that hands that function off to a worker thread via
  `sync_to_async`.
- **Two dev servers, one default port.** `python manage.py runserver` and
  `adk web` both listen on port 8000 by default, so running both at once
  meant whichever started second failed outright. The agent now runs on
  `--port 8001` instead.
- **The model itself can be temporarily unavailable.** Gemini occasionally
  returns a 503 under high demand. Rather than surface that as a failure,
  the agent's model config now retries automatically with exponential
  backoff before giving up.
- **Models get deprecated.** The agent started on `gemini-2.5-flash`,
  which has an October 2026 shutdown date on Google's own deprecation
  schedule — it was moved to `gemini-3.6-flash`, the current stable
  release, to avoid a second forced migration later.
- **Conversations can go stale.** Once a tool result is sitting in the chat
  history, the model can answer from that instead of checking the database
  again — so if a booking gets edited in `/admin/` mid-conversation, the
  agent wouldn't necessarily notice. The system instruction now explicitly
  tells it to re-check current data rather than trust anything already in
  the conversation.

## Deliberate boundaries

The agent is one combined `root_agent`, not split into separate
customer-facing and staff-facing agents — but its tool list isn't
everything the code is capable of. `add_timeslot` exists as a function in
`agent.py` but is intentionally left out of the tools the agent can
actually call: opening new bookable capacity is a bigger action than
closing it, so that one was kept out of reach until staff-only tools sit
behind their own access check, separate from what a customer might be able
to trigger through the same conversation.

## Where things stand now

The site runs as an ordinary Django app with `db.sqlite3` as its data
store, `DJANGO_DEBUG=true` and a development `SECRET_KEY` still in place
(both need changing before this is hosted anywhere but a laptop), and
`CORS_ALLOWED_ORIGINS=*` left open for local testing rather than narrowed
to a real domain yet. The agent sits alongside it as an independent
process that happens to share the same data, reachable either as a
terminal chat or a browser UI, with no write path into the database that
doesn't already go through the same Django models the website itself
relies on.
