import datetime
import json
import urllib.parse
import urllib.request

from django.conf import settings
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

# pyrefly: ignore [missing-import]
from .models import Booking, Service, TimeSlot
import json
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.core.validators import validate_email
from django.core.exceptions import ValidationError
from .models import ContactMessage
from . import services


DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%Y/%m/%d")
TIME_FORMATS = ("%H:%M", "%H:%M:%S", "%I:%M %p")


# --- helpers ----------------------------------------------------------------

def _parse_payload(request):
    """Accept JSON or normal form-encoded POST bodies."""
    if "application/json" in request.content_type:
        try:
            return json.loads(request.body.decode("utf-8") or "{}")
        except ValueError:
            return {}
    return {k: v for k, v in request.POST.items()}


def _parse_date(value):
    for fmt in DATE_FORMATS:
        try:
            return datetime.datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def _parse_time(value):
    for fmt in TIME_FORMATS:
        try:
            return datetime.datetime.strptime(value, fmt).time()
        except ValueError:
            continue
    return None


def _client_ip(request):
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


def _api_key_ok(request):
    """If AGENT_API_KEY is unset, everything is allowed (public form)."""
    expected = settings.AGENT_API_KEY.strip()
    if not expected:
        return True
    provided = request.headers.get("X-API-Key") or ""
    if not provided:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            provided = auth[7:]
    return provided.strip() == expected


def _verify_turnstile(token, remote_ip=None):
    """Returns (ok, error_message)."""
    if not settings.REQUIRE_CAPTCHA:
        return True, ""
    if not settings.TURNSTILE_SECRET:
        return False, "Server misconfiguration: TURNSTILE_SECRET is not set."
    if not token:
        return False, "Please complete the bot check."

    fields = {"secret": settings.TURNSTILE_SECRET, "response": token}
    if remote_ip:
        fields["remoteip"] = remote_ip
    req = urllib.request.Request(
        "https://challenges.cloudflare.com/turnstile/v0/siteverify",
        data=urllib.parse.urlencode(fields).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            body = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        return False, f"Could not reach the verification service ({exc})."

    if body.get("success"):
        return True, ""

    codes = body.get("error-codes", [])
    if "invalid-input-secret" in codes:
        return False, "Server secret misconfiguration — check TURNSTILE_SECRET."
    if "timeout-or-duplicate" in codes:
        return False, "Verification expired. Please refresh and try again."
    if "invalid-input-response" in codes:
        return False, "Invalid challenge token. Please try the check again."
    return False, "Bot verification failed."


# --- endpoints --------------------------------------------------------------

@require_GET
def status_view(request):
    return JsonResponse({
        "status": "online",
        "service": "Luxury Spa Booking API (Django)",
        "frontend_target": settings.FRONTEND_URL,
        "captcha_required": settings.REQUIRE_CAPTCHA,
        "api_key_configured": bool(settings.AGENT_API_KEY),
        "bookings_stored": Booking.objects.count(),
        "endpoints": ["GET /api/status/", "GET /api/services/",
                      "POST /api/book/", "GET /widget.js"],
    })


@require_GET
def services_view(request):
    services = {
        s.code: {
            "name": s.name,
            "duration": s.duration_label,
            "price": float(s.price),
        }
        for s in Service.objects.filter(is_active=True)
    }
    slots = [t.time.strftime("%H:%M") for t in TimeSlot.objects.filter(is_active=True)]
    return JsonResponse({"services": services, "time_slots": slots})


@csrf_exempt
@require_POST
def book_view(request):
    if not _api_key_ok(request):
        return JsonResponse({"success": False, "error": "Invalid or missing API key."},
                            status=401)

    data = _parse_payload(request)
    errors = []

    full_name = str(data.get("fullName") or data.get("full_name") or "").strip()
    if not full_name:
        errors.append("Full name is required.")

    email = str(data.get("email") or "").strip()
    if "@" not in email or "." not in email.split("@")[-1]:
        errors.append("A valid email address is required.")

    phone = str(data.get("phone") or "").strip()
    if len("".join(c for c in phone if c.isdigit())) < 7:
        errors.append("A valid phone number is required.")

    service_code = str(data.get("service") or "").strip()
    service = Service.objects.filter(code=service_code, is_active=True).first()
    if not service:
        available = list(Service.objects.filter(is_active=True)
                         .values_list("code", flat=True))
        errors.append(f"Please choose a spa service. Options: {available}")

    booking_date = _parse_date(str(data.get("date") or "").strip())
    if booking_date is None:
        errors.append("A valid preferred date is required.")
    elif booking_date < datetime.date.today():
        errors.append("The preferred date cannot be in the past.")

    booking_time = _parse_time(str(data.get("time") or "").strip())
    if booking_time is None:
        errors.append("A valid preferred time is required.")
    elif TimeSlot.objects.filter(is_active=True).exists() and \
            not TimeSlot.objects.filter(time=booking_time, is_active=True).exists():
        errors.append("That time slot is not available.")

    consent = data.get("consent")
    consent_given = str(consent).lower() in ("1", "true", "on", "yes") or consent is True
    if not consent_given:
        errors.append("Please agree to the privacy policy to continue.")

    if errors:
        return JsonResponse({
            "success": False,
            "title": "Booking Validation Error",
            "error": "Form validation failed.",
            "details": errors,
        }, status=400)

    captcha_ok, captcha_error = _verify_turnstile(
        data.get("cf-turnstile-response"), _client_ip(request))
    if not captcha_ok:
        return JsonResponse({
            "success": False,
            "title": "Verification Failed",
            "error": "Bot verification failed.",
            "details": [captcha_error],
        }, status=400)

    # --- Waitlist auto-detection: if a CONFIRMED booking already exists for
    # this date + time, save the new one as WAITLISTED instead. ---------------
    slot_taken = Booking.objects.filter(
        date=booking_date,
        time=booking_time,
        status=Booking.Status.CONFIRMED,
    ).exists()

    booking_status = (Booking.Status.WAITLISTED if slot_taken
                      else Booking.Status.CONFIRMED)

    booking = Booking.objects.create(
        full_name=full_name,
        email=email,
        phone=phone,
        service=service,
        service_name=service.name,
        price=service.price,
        duration_minutes=service.duration_minutes,
        date=booking_date,
        time=booking_time,
        therapist=str(data.get("therapist") or "").strip() or "No preference",
        notes=str(data.get("notes") or "").strip(),
        status=booking_status,
        consent_given=True,
        captcha_verified=settings.REQUIRE_CAPTCHA,
        source_ip=_client_ip(request),
        source_url=request.headers.get("Referer", "")[:200],
    )

    if slot_taken:
        return JsonResponse({
            "success": True,
            "waitlisted": True,
            "title": "Added to Waitlist",
            "message": ("You've been added to the waitlist for this time. "
                        "We'll notify you if the slot opens up."),
            "booking": booking.as_api_dict(),
        })

    return JsonResponse({
        "success": True,
        "waitlisted": False,
        "title": "Booking Confirmed",
        "message": ("Thank you for choosing Luxury Spa. Your appointment request "
                    "has been received. Please check your email for details."),
        "booking": booking.as_api_dict(),
    })

@require_GET
def available_times_view(request):
    """Open times for one specific date -- used by the booking page so a
    slot that's already CONFIRMED disappears from the dropdown just for
    that date (it's still open on every other day, not permanently
    removed). GET /api/available-times/?date=YYYY-MM-DD

    Also served at /api/availability/ (see availability_view below) for
    front-end code that expects that URL/shape instead.
    """
    date_str = str(request.GET.get("date") or "").strip()
    booking_date = _parse_date(date_str)
    if booking_date is None:
        return JsonResponse({"success": False, "error": "A valid date (YYYY-MM-DD) is required."},
                            status=400)

    open_times = services.open_times_for_date(booking_date)
    return JsonResponse({
        "success": True,
        "date": date_str,
        "times": [
            {"value": t, "label": datetime.datetime.strptime(t, "%H:%M").strftime("%I:%M %p").lstrip("0")}
            for t in open_times
        ],
    })


@require_GET
def availability_view(request):
    """Same data as available_times_view, at the URL/shape a front-end
    calling GET /api/availability/?date=YYYY-MM-DD is likely to expect --
    a flat "time_slots" list of "HH:MM" strings, matching the shape
    /api/services/ already uses for its own time_slots field. Also
    includes "times" (value/label pairs) so either shape works."""
    date_str = str(request.GET.get("date") or "").strip()
    booking_date = _parse_date(date_str)
    if booking_date is None:
        return JsonResponse({"success": False, "error": "A valid date (YYYY-MM-DD) is required.",
                             "time_slots": []}, status=400)

    open_times = services.open_times_for_date(booking_date)
    return JsonResponse({
        "success": True,
        "date": date_str,
        "time_slots": open_times,
        "times": [
            {"value": t, "label": datetime.datetime.strptime(t, "%H:%M").strftime("%I:%M %p").lstrip("0")}
            for t in open_times
        ],
    })


@csrf_exempt  # Retain your existing CSRF strategy if applicable
@require_POST
def submit_contact(request):
    try:
        data = json.loads(request.body)
        name = data.get('name', '').strip()
        email = data.get('email', '').strip()
        message = data.get('message', '').strip()

        # Security and validation
        if not name or not email or not message:
            return JsonResponse({'success': False, 'error': 'All fields are required.'}, status=400)

        try:
            validate_email(email)
        except ValidationError:
            return JsonResponse({'success': False, 'error': 'Invalid email format.'}, status=400)

        # Save to the 2nd database (ContactMessage table)
        new_msg = ContactMessage.objects.create(
            name=name,
            email=email,
            message=message,
        )

        # Hand the message to the agent: classify intent, and if it's a
        # reschedule request, look up the booking + move it/waitlist it.
        result = services.handle_contact_message(new_msg)

        return JsonResponse({
            'success': True,
            'message': result.get('message', 'Your enquiry has been submitted.'),
            'outcome': result.get('status'),
            'contact_id': new_msg.id,
        })

    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


@require_GET
def widget_js(request):
    """Drop-in JS for the public site: adds Turnstile and posts to this API."""
    api_origin = request.build_absolute_uri("/").rstrip("/")
    confirmation = urllib.parse.urljoin(settings.FRONTEND_URL, "booking-confirmation")
    js = f"""
/** Luxury Spa Booking Widget -> Django backend at {api_origin} */
(function () {{
  var API = "{api_origin}";
  var SITEKEY = "{settings.TURNSTILE_SITEKEY}";
  var REQUIRE_CAPTCHA = {str(settings.REQUIRE_CAPTCHA).lower()};

  window.initSpaBookingAgent = function (config) {{
    config = config || {{}};
    var form = document.getElementById(config.formId || "bookingForm")
            || document.querySelector("form");
    if (!form) return;

    if (REQUIRE_CAPTCHA && SITEKEY) {{
      if (!document.querySelector('script[src*="turnstile/v0/api.js"]')) {{
        var s = document.createElement("script");
        s.src = "https://challenges.cloudflare.com/turnstile/v0/api.js";
        s.async = true; s.defer = true;
        document.head.appendChild(s);
      }}
      var box = document.createElement("div");
      box.className = "cf-turnstile";
      box.setAttribute("data-sitekey", SITEKEY);
      form.insertBefore(box, form.querySelector('button[type="submit"]') || form.lastChild);
    }}

    form.addEventListener("submit", function (e) {{
      e.preventDefault();
      var payload = {{}};
      new FormData(form).forEach(function (v, k) {{ payload[k] = v; }});

      fetch(API + "/api/book/", {{
        method: "POST",
        headers: Object.assign(
          {{ "Content-Type": "application/json" }},
          config.apiKey ? {{ "X-API-Key": config.apiKey }} : {{}}
        ),
        body: JSON.stringify(payload)
      }})
      .then(function (r) {{ return r.json(); }})
      .then(function (res) {{
        if (res.success) {{
          var banner = document.createElement("div");
          banner.style.cssText = "position:fixed;top:0;left:0;width:100%;padding:16px;"
            + "background:#22c55e;color:#fff;text-align:center;z-index:9999;font-weight:bold;";
          banner.innerText = "Booking confirmed! Ref: " + res.booking.booking_id;
          document.body.prepend(banner);
          setTimeout(function () {{ window.location.href = "{confirmation}"; }}, 1500);
        }} else {{
          alert(res.details ? res.details.join("\\n") : res.error);
          if (window.turnstile) window.turnstile.reset();
        }}
      }})
      .catch(function (err) {{ alert("Connection error: " + err); }});
    }});
  }};
}})();
"""
    return HttpResponse(js, content_type="application/javascript")


# --- local test pages -------------------------------------------------------

def booking_form(request):
    """A local copy of the public booking form, served over the same origin.

    Same origin means no CORS and no mixed-content blocking, so you can test
    the full flow on your laptop without a tunnel.
    """
    from django.shortcuts import render
    return render(request, "booking.html", {
        "services": Service.objects.filter(is_active=True),
        "slots": [t.time for t in TimeSlot.objects.filter(is_active=True)],
        "sitekey": settings.TURNSTILE_SITEKEY if settings.REQUIRE_CAPTCHA else "",
        "require_captcha": settings.REQUIRE_CAPTCHA,
    })


def confirmation(request):
    from django.shortcuts import render
    return render(request, "confirmation.html", {"ref": request.GET.get("ref", "")})


def contact_form(request):
    """A local copy of the public contact form, served over the same origin
    -- same idea as booking_form() above, for testing without a tunnel."""
    from django.shortcuts import render
    return render(request, "contact.html", {})
