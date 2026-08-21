import csv

from django import forms
from django.contrib import admin, messages
from django.db.models import Count
from django.http import HttpResponse
from django.utils import timezone
from django.utils.html import format_html

# pyrefly: ignore [missing-import]
from .models import Booking, Service, TimeSlot, ContactMessage

admin.site.site_header = "Luxury Spa — Booking Administration"
admin.site.site_title = "Luxury Spa Admin"
admin.site.index_title = "Appointments & services"


# --- Dashboard hero stats (shown on the /admin/ index page) ----------------
_default_admin_index = admin.site.index


def _spa_admin_index(request, extra_context=None):
    extra_context = extra_context or {}
    today = timezone.localdate()
    extra_context["dashboard_stats"] = [
        {"label": "Total bookings", "count": Booking.objects.count(), "color": "#EAF1EC"},
        {
            "label": "Waitlisted",
            "count": Booking.objects.filter(status=Booking.Status.WAITLISTED).count(),
            "color": "#FCEACB",
        },
        {
            "label": "Today's appointments",
            "count": Booking.objects.filter(date=today)
            .exclude(status=Booking.Status.CANCELLED)
            .count(),
            "color": "#DCEDDC",
        },
        {
            "label": "Active services",
            "count": Service.objects.filter(is_active=True).count(),
            "color": "#DCE4F5",
        },
    ]
    return _default_admin_index(request, extra_context)


admin.site.index = _spa_admin_index


@admin.register(Service)
class ServiceAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "duration_minutes", "price", "is_active", "booking_count")
    list_editable = ("price", "is_active")
    list_filter = ("is_active",)
    search_fields = ("name", "code")
    ordering = ("sort_order", "name")

    @admin.display(description="Bookings")
    def booking_count(self, obj):
        return obj.bookings.count()


@admin.register(TimeSlot)
class TimeSlotAdmin(admin.ModelAdmin):
    list_display = ("time", "is_active")
    list_editable = ("is_active",)


class BookingAdminForm(forms.ModelForm):
    """Forces 24-hour time entry/display so it matches the booking form exactly."""

    class Meta:
        model = Booking
        fields = "__all__"
        widgets = {
            "time": forms.TimeInput(format="%H:%M", attrs={"type": "time"}),
            "date": forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
        }

class StatusFilter(admin.SimpleListFilter):
    """Same as the default 'status' filter, minus the No show option."""
    title = "status"
    parameter_name = "status"

    def lookups(self, request, model_admin):
        return [
            (value, label)
            for value, label in Booking.Status.choices
            if value != Booking.Status.NO_SHOW
        ]

    def queryset(self, request, queryset):
        if self.value():
            return queryset.filter(status=self.value())
        return queryset



@admin.register(Booking)
class BookingAdmin(admin.ModelAdmin):
    form = BookingAdminForm

    class ContactMessageInline(admin.TabularInline):
        model = ContactMessage
        fk_name = "related_booking"
        extra = 0
        can_delete = False
        fields = ("created_at", "status", "requested_date", "agent_reply")
        readonly_fields = fields
        verbose_name = "Contact message that affected this booking"
        verbose_name_plural = "Contact messages that affected this booking"

        def has_add_permission(self, request, obj=None):
            return False

    inlines = [ContactMessageInline]

    # --- List view (the owner's main screen) --------------------------------
    list_display = (
        "booking_id", "date_display", "time_display", "full_name",
        "service_name", "phone", "status_badge", "created_at",
    )
    list_filter = (StatusFilter, "service", "date", "captcha_verified")
    search_fields = ("booking_id", "full_name", "email", "phone", "notes")
    date_hierarchy = "date"
    ordering = ("-created_at",)
    list_per_page = 50
    actions = ("mark_confirmed", "mark_completed", "mark_cancelled",
               "move_to_waitlist", "promote_from_waitlist", "export_csv")

    # --- Detail view --------------------------------------------------------
    readonly_fields = (
        "booking_id", "created_at", "updated_at", "captcha_verified",
        "consent_given", "source_ip", "source_url",
    )
    fieldsets = (
        ("Reference", {
            "fields": ("booking_id", "status", ("created_at", "updated_at")),
        }),
        ("Customer", {
            "fields": ("full_name", "email", "phone"),
        }),
        ("Appointment", {
            "fields": ("service", "service_name", "duration_minutes", "price",
                       ("date", "time"), "therapist", "notes"),
        }),
        ("Staff", {
            "fields": ("internal_notes",),
        }),
        ("Submission audit", {
            "classes": ("collapse",),
            "fields": ("consent_given", "captcha_verified", "source_ip", "source_url"),
        }),
    )

    @admin.display(description="Date", ordering="date")
    def date_display(self, obj):
        return obj.date.strftime("%Y-%m-%d")

    @admin.display(description="Time", ordering="time")
    def time_display(self, obj):
        """Exactly the value the customer selected, e.g. 14:30."""
        return obj.time.strftime("%H:%M")

    @admin.display(description="Status", ordering="status")
    def status_badge(self, obj):
        colours = {
            "PENDING": "#b45309",
            "CONFIRMED": "#15803d",
            "COMPLETED": "#1d4ed8",
            "CANCELLED": "#b91c1c",
            "NO_SHOW": "#6b7280",
            "WAITLISTED": "#d97706",
        }
        return format_html(
            '<span class="status-pill" style="background:{};">{}</span>',
            colours.get(obj.status, "#6b7280"),
            obj.get_status_display(),
        )

    # --- Bulk actions -------------------------------------------------------
    def _bulk_status(self, request, queryset, status, label):
        updated = queryset.update(status=status)
        self.message_user(request, f"{updated} booking(s) marked {label}.",
                          messages.SUCCESS)

    @admin.action(description="Mark selected as Confirmed")
    def mark_confirmed(self, request, queryset):
        self._bulk_status(request, queryset, Booking.Status.CONFIRMED, "confirmed")

    @admin.action(description="Mark selected as Completed")
    def mark_completed(self, request, queryset):
        self._bulk_status(request, queryset, Booking.Status.COMPLETED, "completed")

    @admin.action(description="Mark selected as Cancelled")
    def mark_cancelled(self, request, queryset):
        self._bulk_status(request, queryset, Booking.Status.CANCELLED, "cancelled")

    @admin.action(description="Move selected to Waitlist")
    def move_to_waitlist(self, request, queryset):
        self._bulk_status(request, queryset, Booking.Status.WAITLISTED, "waitlisted")

    @admin.action(description="Promote from waitlist to Confirmed")
    def promote_from_waitlist(self, request, queryset):
        self._bulk_status(request, queryset, Booking.Status.CONFIRMED, "confirmed (promoted from waitlist)")

    @admin.action(description="Export selected to CSV")
    def export_csv(self, request, queryset):
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="bookings.csv"'
        writer = csv.writer(response)
        writer.writerow([
            "Booking ID", "Status", "Date", "Time", "Name", "Email", "Phone",
            "Service", "Duration (min)", "Price", "Therapist", "Notes",
            "Submitted at",
        ])
        for b in queryset.select_related("service"):
            writer.writerow([
                b.booking_id, b.get_status_display(), b.date,
                b.time.strftime("%H:%M"), b.full_name, b.email, b.phone,
                b.service_name, b.duration_minutes, b.price, b.therapist,
                b.notes, b.created_at.strftime("%Y-%m-%d %H:%M"),
            ])
        return response

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("service")

    # --- Custom "N bookings, N waitlisted" subtitle + slot-clash footnote ---
    def changelist_view(self, request, extra_context=None):
        extra_context = extra_context or {}

        total = Booking.objects.count()
        waitlisted_count = Booking.objects.filter(status=Booking.Status.WAITLISTED).count()
        booking_word = "booking" if total == 1 else "bookings"
        subtitle = f"{total} {booking_word}"
        if waitlisted_count:
            subtitle += f", {waitlisted_count} waitlisted"
        extra_context["subtitle"] = subtitle

        # Stat strip: a colored count card per status, shown above the table.
        status_colours = {
            Booking.Status.PENDING: "#b45309",
            Booking.Status.CONFIRMED: "#15803d",
            Booking.Status.COMPLETED: "#1d4ed8",
            Booking.Status.CANCELLED: "#b91c1c",
            Booking.Status.NO_SHOW: "#6b7280",
            Booking.Status.WAITLISTED: "#d97706",
        }
        counts_by_status = dict(
            Booking.objects.values_list("status").annotate(n=Count("id"))
        )
        extra_context["status_stats"] = [
            {
                "label": "All bookings",
                "count": total,
                "color": "#5C6E5D",
                "status": "",
            }
        ] + [
            {
                "label": label,
                "count": counts_by_status.get(value, 0),
                "color": status_colours.get(value, "#6b7280"),
                "status": value,
            }
            for value, label in Booking.Status.choices
            if value != Booking.Status.NO_SHOW
        ]

        # Detect rows that were auto-waitlisted because an earlier row (in the
        # currently displayed order) already occupies the same date + time.
        ordered = list(self.get_queryset(request).order_by(*(self.ordering or ("-created_at",))))
        notes = []
        for idx, booking in enumerate(ordered, start=1):
            if booking.status != Booking.Status.WAITLISTED:
                continue
            for jdx in range(idx - 2, -1, -1):
                earlier = ordered[jdx]
                if earlier.date == booking.date and earlier.time == booking.time:
                    notes.append(
                        f"Row {idx} shares the same slot as row {jdx + 1} "
                        f"({booking.time.strftime('%H:%M')}, {booking.service_name}) "
                        f"— auto-waitlisted on submission."
                    )
                    break
        extra_context["waitlist_slot_notes"] = notes

        return super().changelist_view(request, extra_context=extra_context)


# --- "2nd database": Contact us messages + reschedule waitlist -------------
# Same admin theme as bookings above (the CSS in admin_custom.css is loaded
# site-wide via templates/admin/base_site.html, so no extra styling needed
# here -- reusing the .status-pill class is enough to match the look).

_CONTACT_STATUS_COLOURS = {
    "PENDING": "#b45309",
    "PROCESSING": "#1d4ed8",
    "PROCESSED": "#15803d",
    "WAITING_FOR_CUSTOMER": "#d97706",
    "FAILED": "#b91c1c",
}


@admin.register(ContactMessage)
class ContactMessageAdmin(admin.ModelAdmin):
    list_display = ("name", "email", "intent", "status_badge", "requested_date",
                     "linked_booking", "short_message", "created_at")
    list_filter = ("status", "intent")
    search_fields = ("name", "email", "message", "related_booking__booking_id")
    ordering = ("-created_at",)
    date_hierarchy = "created_at"
    list_per_page = 50
    readonly_fields = ("created_at", "agent_reply", "email_preview",
                       "related_booking", "requested_date")
    autocomplete_fields = ()
    actions = ("reprocess_selected",)

    fieldsets = (
        ("Customer", {"fields": ("name", "email")}),
        ("Message", {"fields": ("message",)}),
        ("Agent", {"fields": ("status", "intent", "agent_reply", "created_at")}),
        ("What changed in the database", {
            "fields": ("related_booking", "requested_date"),
            "description": "If this message resulted in a booking being "
                           "moved or waitlisted, that booking is linked here.",
        }),
        ("Notification email (dummy until EMAIL_HOST is configured)", {
            "classes": ("collapse",),
            "fields": ("email_preview",),
        }),
    )

    @admin.display(description="Message")
    def short_message(self, obj):
        text = obj.message or ""
        return text if len(text) <= 60 else text[:57] + "..."

    @admin.display(description="Status", ordering="status")
    def status_badge(self, obj):
        return format_html(
            '<span class="status-pill" style="background:{};">{}</span>',
            _CONTACT_STATUS_COLOURS.get(obj.status, "#6b7280"),
            obj.get_status_display(),
        )

    @admin.display(description="Booking affected", ordering="related_booking__booking_id")
    def linked_booking(self, obj):
        if not obj.related_booking_id:
            return "—"
        booking = obj.related_booking
        url = f"/admin/bookings/booking/{booking.id}/change/"
        return format_html(
            '<a href="{}">{}</a> <span class="status-pill" style="background:{};">{}</span>',
            url, booking.booking_id,
            "#15803d" if booking.status == "CONFIRMED" else "#8a6300",
            booking.get_status_display(),
        )

    @admin.action(description="Re-run the agent on selected messages")
    def reprocess_selected(self, request, queryset):
        from . import services
        processed = 0
        for msg in queryset:
            services.handle_contact_message(msg)
            processed += 1
        self.message_user(request, f"Re-processed {processed} message(s).", messages.SUCCESS)
