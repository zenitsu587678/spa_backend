from django.urls import path

from . import views

urlpatterns = [
    path("api/status/", views.status_view, name="status"),
    path("api/services/", views.services_view, name="services"),
    path("api/available-times/", views.available_times_view, name="available-times"),
    path("api/availability/", views.availability_view, name="availability"),
    path("api/book/", views.book_view, name="book"),
    path("api/contact/", views.submit_contact, name="contact"),
    path("widget.js", views.widget_js, name="widget-js"),

    # Slash-less aliases so a front-end calling without a trailing slash still works.
    path("api/status", views.status_view),
    path("api/services", views.services_view),
    path("api/available-times", views.available_times_view),
    path("api/availability", views.availability_view),
    path("api/book", views.book_view),
    path("api/contact", views.submit_contact),

    # Local copy of the public booking form, for testing without a tunnel.
    path("book/", views.booking_form, name="booking-form"),
    path("booking-confirmation/", views.confirmation, name="confirmation"),

    # Local copy of the public "Contact us" form, for testing without a tunnel.
    path("contact/", views.contact_form, name="contact-form"),
]
