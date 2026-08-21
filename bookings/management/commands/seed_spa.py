"""Loads the default service menu and time slots. Safe to re-run."""

import datetime

from django.core.management.base import BaseCommand

from bookings.models import Service, TimeSlot

SERVICES = [
    ("swedish", "Swedish Massage", 60, 120),
    ("deep-tissue", "Deep Tissue Massage", 60, 140),
    ("aromatherapy", "Aromatherapy Massage", 90, 160),
    ("hot-stone", "Hot Stone Massage", 90, 175),
    ("facial", "Signature Facial Treatment", 60, 130),
    ("body-scrub", "Revitalizing Body Scrub", 45, 95),
    ("body-wrap", "Detoxifying Body Wrap", 60, 115),
    ("reflexology", "Foot Reflexology", 30, 65),
]


class Command(BaseCommand):
    help = "Seeds spa services and 30-minute time slots from 09:00 to 19:30."

    def handle(self, *args, **options):
        for order, (code, name, duration, price) in enumerate(SERVICES):
            Service.objects.update_or_create(
                code=code,
                defaults={
                    "name": name,
                    "duration_minutes": duration,
                    "price": price,
                    "sort_order": order,
                },
            )
        self.stdout.write(self.style.SUCCESS(f"{len(SERVICES)} services ready."))

        slot = datetime.time(9, 0)
        created = 0
        current = datetime.datetime.combine(datetime.date.today(), slot)
        end = datetime.datetime.combine(datetime.date.today(), datetime.time(19, 30))
        while current <= end:
            _, made = TimeSlot.objects.get_or_create(time=current.time())
            created += int(made)
            current += datetime.timedelta(minutes=30)
        self.stdout.write(self.style.SUCCESS(f"Time slots ready ({created} new)."))
