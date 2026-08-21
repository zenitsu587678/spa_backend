"""Seeds four demo bookings that reproduce the reference screenshot exactly:

    SPA-2026-7085  Emily Watson   2026-08-16 14:30  Hot Stone Massage   Confirmed
    SPA-2026-6634  Marcus Tan     2026-08-16 14:30  Hot Stone Massage   Waitlisted
    SPA-2026-4521  Priya Nair     2026-08-17 10:00  Swedish Massage     Pending
    SPA-2026-9902  James Lee      2026-08-15 16:00  Signature Facial Treatment  Completed

Marcus's booking shares Emily's 14:30 Hot Stone slot, which is exactly the
scenario bookings/views.py auto-waitlists on submission.

Safe to re-run: existing bookings with the same booking_id are updated in
place rather than duplicated. Pass --reset to remove every other booking
first, so the table shows only these four (matching "4 bookings, 1
waitlisted" in the screenshot).
"""

import datetime

from django.core.management.base import BaseCommand
from django.utils import timezone

from bookings.models import Booking, Service

DEMO_BOOKINGS = [
    # booking_id,     full_name,      email,                      phone,             service_code, date,               time,     status,       days_ago (created_at)
    ("SPA-2026-9902", "James Lee",    "james.lee@example.com",    "+65 9012 3344", "facial",     datetime.date(2026, 8, 15), datetime.time(16, 0), Booking.Status.COMPLETED,  4),
    ("SPA-2026-4521", "Priya Nair",   "priya.nair@example.com",   "+65 9876 5432", "swedish",    datetime.date(2026, 8, 17), datetime.time(10, 0), Booking.Status.PENDING,    3),
    ("SPA-2026-6634", "Marcus Tan",   "marcus.tan@example.com",   "+65 8234 1290", "hot-stone",  datetime.date(2026, 8, 16), datetime.time(14, 30), Booking.Status.WAITLISTED, 2),
    ("SPA-2026-7085", "Emily Watson", "emily.watson@example.com", "+65 9123 4567", "hot-stone",  datetime.date(2026, 8, 16), datetime.time(14, 30), Booking.Status.CONFIRMED,  1),
]


class Command(BaseCommand):
    help = "Seeds the four demo bookings shown in the admin reference screenshot."

    def add_arguments(self, parser):
        parser.add_argument(
            "--reset", action="store_true",
            help="Delete every other booking first, leaving only these four.",
        )

    def handle(self, *args, **options):
        if options["reset"]:
            deleted, _ = Booking.objects.exclude(
                booking_id__in=[row[0] for row in DEMO_BOOKINGS]
            ).delete()
            if deleted:
                self.stdout.write(self.style.WARNING(f"Removed {deleted} other booking(s)."))

        now = timezone.now()
        for booking_id, full_name, email, phone, service_code, date, time, status, days_ago in DEMO_BOOKINGS:
            service = Service.objects.get(code=service_code)
            booking, created = Booking.objects.update_or_create(
                booking_id=booking_id,
                defaults={
                    "full_name": full_name,
                    "email": email,
                    "phone": phone,
                    "service": service,
                    "service_name": service.name,
                    "price": service.price,
                    "duration_minutes": service.duration_minutes,
                    "date": date,
                    "time": time,
                    "status": status,
                    "consent_given": True,
                    "captcha_verified": True,
                },
            )
            # created_at/updated_at are auto_now(_add) so set them directly at
            # the DB level to control display order (list is sorted newest-first).
            Booking.objects.filter(pk=booking.pk).update(
                created_at=now - datetime.timedelta(days=days_ago),
                updated_at=now - datetime.timedelta(days=days_ago),
            )
            verb = "Created" if created else "Updated"
            self.stdout.write(f"{verb} {booking_id} — {full_name} ({status}).")

        self.stdout.write(self.style.SUCCESS("Demo bookings ready."))
