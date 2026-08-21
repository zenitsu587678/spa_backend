import datetime
import random

from django.db import models


class Service(models.Model):
    """A bookable spa treatment. Editable by the owner in the admin."""

    code = models.SlugField(max_length=50, unique=True,
                            help_text="Value the website form submits, e.g. 'hot-stone'")
    name = models.CharField(max_length=120)
    duration_minutes = models.PositiveIntegerField(default=60)
    price = models.DecimalField(max_digits=8, decimal_places=2)
    is_active = models.BooleanField(default=True,
                                    help_text="Uncheck to hide from the booking form")
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "name"]

    def __str__(self):
        return f"{self.name} ({self.duration_minutes} min)"

    @property
    def duration_label(self):
        return f"{self.duration_minutes} min"


class TimeSlot(models.Model):
    """An openable/closable appointment slot, e.g. 09:00."""

    time = models.TimeField(unique=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["time"]

    def __str__(self):
        return self.time.strftime("%H:%M")
class ContactMessage(models.Model):
    STATUS_CHOICES = [
        ('PENDING', 'Pending'),
        ('PROCESSING', 'Processing'),
        ('PROCESSED', 'Processed'),
        ('WAITING_FOR_CUSTOMER', 'Waiting for Customer'),
        ('FAILED', 'Failed'),
    ]

    # Matching the frontend fields conceptually
    name = models.CharField(max_length=255)
    email = models.EmailField()
    message = models.TextField()
    
    # System fields
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='PENDING')
    intent = models.CharField(max_length=50, blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    # What the agent decided and told the customer -- lets you confirm in the
    # admin that a message was received and see exactly how it was handled,
    # without needing to watch the server console.
    agent_reply = models.TextField(
        blank=True, help_text="The message the agent sent back to the customer.")
    email_preview = models.TextField(
        blank=True,
        help_text=("The notification email content. Shown here while email "
                    "sending is a dummy/stub; once EMAIL_HOST is configured "
                    "in settings.py this is only a copy of what was really sent."))

    # Real database links to what actually happened -- so you can see it in
    # the database itself, not just read it back as text. Set only for
    # reschedule requests that matched an existing booking.
    related_booking = models.ForeignKey(
        "Booking", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="contact_messages",
        help_text="The booking this message ended up changing, if any.")
    requested_date = models.DateField(
        null=True, blank=True,
        help_text="The new date the customer asked for, if this was a reschedule request.")

    def __str__(self):
        return f"{self.name} - {self.email} ({self.status})"


def generate_booking_id() -> str:
    year = datetime.date.today().year
    for _ in range(50):
        candidate = f"SPA-{year}-{random.randint(1000, 9999)}"
        if not Booking.objects.filter(booking_id=candidate).exists():
            return candidate
    # Extremely unlikely fallback
    return f"SPA-{year}-{random.randint(100000, 999999)}"


class Booking(models.Model):
    """One submission from the website booking form."""

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        CONFIRMED = "CONFIRMED", "Confirmed"
        COMPLETED = "COMPLETED", "Completed"
        CANCELLED = "CANCELLED", "Cancelled"
        NO_SHOW = "NO_SHOW", "No show"
        WAITLISTED = "WAITLISTED", "Waitlisted"

    booking_id = models.CharField(max_length=32, unique=True,
                                  default=generate_booking_id, editable=False)

    # Customer
    full_name = models.CharField("full name", max_length=150)
    email = models.EmailField()
    phone = models.CharField(max_length=40)

    # Appointment
    service = models.ForeignKey(Service, on_delete=models.PROTECT,
                                related_name="bookings")
    service_name = models.CharField(max_length=120, blank=True,
                                    help_text="Snapshot at time of booking")
    price = models.DecimalField(max_digits=8, decimal_places=2, default=0,
                                help_text="Snapshot at time of booking")
    duration_minutes = models.PositiveIntegerField(default=60)
    date = models.DateField("preferred date")
    time = models.TimeField("preferred time")
    therapist = models.CharField(max_length=120, blank=True, default="No preference")
    notes = models.TextField(blank=True)

    # Admin / audit
    status = models.CharField(max_length=20, choices=Status.choices,
                              default=Status.CONFIRMED)
    internal_notes = models.TextField(blank=True,
                                      help_text="Staff only — not shown to the customer")
    consent_given = models.BooleanField(default=False)
    captcha_verified = models.BooleanField(default=False)
    source_ip = models.GenericIPAddressField(null=True, blank=True)
    source_url = models.URLField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["date", "time"]),
            models.Index(fields=["status"]),
        ]

    def __str__(self):
        return f"{self.booking_id} — {self.full_name} — {self.service_name}"

    def save(self, *args, **kwargs):
        if self.service_id and not self.service_name:
            self.service_name = self.service.name
            self.price = self.service.price
            self.duration_minutes = self.service.duration_minutes
        super().save(*args, **kwargs)

    @property
    def slot_label(self):
        return f"{self.date:%d %b %Y} at {self.time:%H:%M}"

    def as_api_dict(self):
        return {
            "booking_id": self.booking_id,
            "customer": {
                "full_name": self.full_name,
                "email": self.email,
                "phone": self.phone,
            },
            "appointment": {
                "service_code": self.service.code,
                "service_name": self.service_name,
                "duration": f"{self.duration_minutes} min",
                "price": f"${self.price:.2f}",
                "date": self.date.isoformat(),
                "time": self.time.strftime("%H:%M"),
                "therapist": self.therapist,
                "notes": self.notes,
            },
            "status": self.status,
            "waitlisted": self.status == self.Status.WAITLISTED,
            "captcha_verified": self.captcha_verified,
            "created_at": self.created_at.isoformat(),
        }
