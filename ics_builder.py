"""Builds the actual .ics (iCalendar) file content for a digest event.

Earlier versions of this project used a plain
https://calendar.google.com/calendar/render link instead - an ordinary
hyperlink, no server hosting needed, and critically (unlike a
data:text/calendar link) not stripped by Gmail's HTML sanitizer, which
does strip data: URIs from received email links. That worked, but it only
ever offered to add the event to *Google* Calendar - on an iPhone, tapping
it opens Google Calendar's web page rather than the native Calendar app's
own "Add Event" sheet.

Joe asked for the iPhone-native experience instead, so as of round 17 the
digest links to a small dashboard endpoint (see web_app.py's `/ics/<token>`
route and ics_store.py) that serves the real .ics bytes built here with a
`text/calendar` content type - tapping that link is what triggers the
native "Add to Calendar" prompt on iOS (and works the same way, via
whatever calendar app is registered, on Android/desktop). `build_ics()`
below is the only function still used for that; the old Google Calendar
link builder has been removed now that nothing calls it.

Round 21 (Joe: tapping the link was still prompting to "subscribe" rather
than add a one-off event, and asked to "ensure event logic is sturdy and is
actually an event inviting attendance or participation") rebuilt the event
as a genuine one-off *invitation* rather than a passive announcement:
METHOD:REQUEST (not PUBLISH) plus a real ORGANIZER/ATTENDEE pair - the
combination iTIP (RFC 5546) actually defines for "here is one specific
occurrence, please add/respond to it", which is unambiguous in a way a bare
METHOD-less or PUBLISH-only file apparently still wasn't for every calendar
app that was tried. The organizer is the Gmail account doing the sorting
(effectively "your assistant invited you"); the attendee is whoever the
digest itself was actually sent to - see pipeline.py's call site.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y%m%dT%H%M%S")


def build_ics(
    title: str,
    start_iso: str,
    end_iso: str = "",
    location: str = "",
    description: str = "",
    organizer_email: str = "",
    attendee_email: str = "",
) -> bytes | None:
    """Returns raw .ics bytes for a one-off event *invitation*, or None if
    start_iso couldn't be parsed (caller should just omit the Add to
    Calendar link in that case). `organizer_email`/`attendee_email`, when
    given, add real ORGANIZER/ATTENDEE properties and switch the calendar's
    METHOD to REQUEST - see the module docstring for why that's what
    actually makes this read as "one event to add", not a calendar to
    subscribe to."""
    try:
        start = datetime.fromisoformat(start_iso)
    except (ValueError, TypeError):
        return None

    if end_iso:
        try:
            end = datetime.fromisoformat(end_iso)
        except ValueError:
            end = start + timedelta(hours=1)
    else:
        end = start + timedelta(hours=1)

    uid = f"{uuid.uuid4()}@gmail-ai-sorter"
    now = _fmt(datetime.utcnow()) + "Z"
    # REQUEST is the iTIP method for "here's one occurrence, please add it
    # (and you could reply)" - it's what makes an .ics genuinely read as an
    # invitation to attend rather than an ambiguous calendar resource, which
    # is what a calendar app can otherwise interpret as an ongoing feed to
    # subscribe to instead of a single event to import. PUBLISH (a plain
    # announcement, no organizer/attendee, no expected reply) is kept as a
    # fallback for any caller that doesn't have real participant addresses
    # to hand - there's no true attendee to invite without at least one.
    method = "REQUEST" if (organizer_email and attendee_email) else "PUBLISH"

    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;").replace("\n", "\\n")

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//gmail-ai-sorter//EN",
        "CALSCALE:GREGORIAN",
        f"METHOD:{method}",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{now}",
        f"DTSTART:{_fmt(start)}",
        f"DTEND:{_fmt(end)}",
        f"SUMMARY:{esc(title)}",
        # STATUS/SEQUENCE/TRANSP make this look like a real, confirmed,
        # busy-time event rather than a bare date marker - part of Joe's
        # "sturdy event logic" ask. SEQUENCE:0 is also required by iTIP for
        # a first-time REQUEST (a later update to the same UID would bump
        # it, though this app never re-sends the same event).
        "STATUS:CONFIRMED",
        "SEQUENCE:0",
        "TRANSP:OPAQUE",
    ]
    if organizer_email:
        lines.append(f"ORGANIZER;CN=Gmail AI Sorter:mailto:{organizer_email}")
    if attendee_email:
        lines.append(
            f"ATTENDEE;CN={esc(attendee_email)};ROLE=REQ-PARTICIPANT;"
            f"PARTSTAT=NEEDS-ACTION;RSVP=TRUE:mailto:{attendee_email}"
        )
    if location:
        lines.append(f"LOCATION:{esc(location)}")
    if description:
        lines.append(f"DESCRIPTION:{esc(description)}")
    lines += ["END:VEVENT", "END:VCALENDAR"]

    # CRLF per RFC 5545.
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")
