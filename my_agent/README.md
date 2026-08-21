# my_agent

An [ADK](https://google.github.io/adk-docs/) agent that lets you talk to your
spa's real booking data in plain English — "what's open Thursday", "book
Jane in for a facial", "who's on the waitlist" — instead of clicking through
Django admin.

It's a single file, `agent.py`, with no database of its own: every tool call
reads or writes the exact same `db.sqlite3` your Django site uses, through
the same models (`bookings/models.py`).

## Quick start

```bash
cd spa_backend          # same folder as manage.py
pip install google-adk
cp my_agent/.env.example my_agent/.env   # then paste in your Gemini API key

python manage.py migrate      # if you haven't already
adk run my_agent               # terminal chat
# or
adk web --port 8001            # browser UI — 8001 because Django's
                                # runserver already owns port 8000
```

Try: *"What services do you have?"*, *"What's open on 2026-08-20?"*,
*"Book Jane Doe, jane@example.com, 555-1234 for a Swedish massage on
2026-08-20 at 10am."*

## How a request flows through the code

```
You type a message
  → ADK sends it to the Gemini API over HTTPS, along with a schema
    describing every tool (built from each function's name/args/docstring)
  → Gemini decides which tool to call and with what arguments, and
    sends that decision back
  → ADK finds the matching Python function in this file and calls it
  → the tool's async wrapper hands off to a worker thread (see "Why
    async?" below)
  → that thread runs a normal Django ORM query against db.sqlite3
  → the result (a plain dict) travels back up: thread → wrapper → ADK
  → ADK sends that dict to Gemini in a second API call
  → Gemini turns it into a natural-language reply, which is what you see
```

Nothing about your database schema or code is ever sent to Gemini — only
the tool docstrings (what each tool does) and whatever a tool call returns
(the actual data).

## File-by-file walkthrough of `agent.py`

**Django bootstrap (top of the file)**
```python
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "spabackend.settings")
django.setup()
```
Outside of `manage.py`, Django doesn't know it's supposed to be running.
This does by hand what `manage.py` normally does for you — load
`INSTALLED_APPS`, wire up the ORM — so `from bookings.models import Booking`
a few lines later actually works.

**Retry config**
```python
_RETRY_CONFIG = types.GenerateContentConfig(
    http_options=types.HttpOptions(retry_options=types.HttpRetryOptions(...)),
)
```
Configures the Gemini HTTP client to automatically retry with exponential
backoff on 503/429/500/502/504 — so a "model overloaded" blip retries
itself instead of failing your request.

**Why every tool is `async def` + `sync_to_async`**

`adk web` runs inside an asyncio event loop. Django refuses to run
synchronous ORM queries directly on that loop (`SynchronousOnlyOperation`)
to protect against corrupting shared connections. So every tool here is
split in two:

```python
def _get_service_info_sync(service_name: str = "") -> dict:
    ...  # the real Service.objects.filter(...) query

async def get_service_info(service_name: str = "") -> dict:
    """docstring — this is what Gemini reads to know what the tool does"""
    return await sync_to_async(_get_service_info_sync, thread_sensitive=True)(service_name)
```

The private `_..._sync` function is where the actual ORM logic lives. The
public `async def` version is the one-liner ADK actually sees — it hands
the real work to a dedicated worker thread via `sync_to_async`, which is
safe for the ORM. `thread_sensitive=True` reuses one consistent thread
across calls rather than spawning a new one each time, which SQLite prefers.

If you add a new tool, follow the same shape.

**The tools**

`add_timeslot` is defined in this file but deliberately left out of
`root_agent`'s `tools` list — opening a new recurring daily time is a
bigger schedule change than closing one, so it's kept out of what this
agent (reachable by customers) can do on its own. Wire it back in only once
this agent is split so staff-only tools sit behind their own access check
(see "Known limitations" below).

| Tool | What it touches | Notes |
|---|---|---|
| `get_service_info` | `Service` | list/search active services & pricing |
| `check_availability` | `TimeSlot`, `Booking` | open times not already `CONFIRMED` on a date |
| `book_slot` | `Booking` | confirms, or waitlists if the slot's taken |
| `cancel_booking` | `Booking` | cancels by `booking_id`; auto-promotes the oldest waitlisted booking for that date/time |
| `reschedule_booking` | `Booking` | `await`s `cancel_booking` then `book_slot` internally |
| `block_timeslot` *(staff)* | `TimeSlot` | closes a recurring time; existing bookings unaffected |
| `list_bookings` *(staff)* | `Booking` | filter by date/status |
| `get_waitlist` *(staff)* | `Booking` | who's waiting, oldest first |

`TimeSlot` in your model is a recurring daily template (just a `time`, no
date/staff) — so "closing 2pm" closes 2pm every day, not just one date.

**`root_agent`**
```python
root_agent = Agent(
    model="gemini-3.6-flash",
    generate_content_config=_RETRY_CONFIG,
    tools=[get_service_info, check_availability, ...],
    instruction="...",
)
```
This is the only name ADK looks for when it imports this file. `tools` is
a list of the actual Python function objects — ADK introspects each one at
startup to build what Gemini sees. `instruction` is the system prompt: the
*rules* for behavior (waitlist wording, always re-check current data,
don't invent details). The tool docstrings are the rules for *what each
tool does* — together that's the entirety of what Gemini knows.

## Known limitations / things to watch for

- **One combined agent, not two.** Both customer tools (`book_slot`, etc.)
  and staff tools (`block_timeslot`, `list_bookings`, `get_waitlist`) are
  in the same `root_agent`, so anyone talking to it can call either.
  `add_timeslot` is defined but intentionally not registered, since opening
  new schedule capacity is a bigger change than closing it — before giving
  customers access, split staff-only tools (including re-adding
  `add_timeslot`) into a second `Agent` gated behind auth.
- **Stale answers within one chat.** Once a tool result is in the
  conversation history, the model can answer from that instead of
  re-calling the tool — so if you edit a booking in Django admin mid-chat,
  ask the agent to "check again" rather than assuming it already knows.
- **Email is a console-log stub.** `_send_email()` just prints; wire up
  real `EMAIL_HOST`/etc. in `spabackend/settings.py` and swap the body of
  that function for `django.core.mail.send_mail` when you're ready.
- **Port collision with Django.** `python manage.py runserver` and
  `adk web` both default to port 8000 — run `adk web --port 8001` (or move
  Django to a different port) to run both at once.
- **`gemini-3.6-flash` will eventually get a deprecation date too.** Check
  Google's model deprecation page occasionally and update the `model=`
  string in `root_agent` when it does.
