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

Round 28 (Joe: real iPhone testing on Apple Mail still showed a "subscribe"
prompt rather than a one-off "Add Event", even with everything round 21
already covered): found one more genuine spec gap while digging into this -
DTSTART/DTEND were being written as "floating" local time (no "Z", no
TZID at all), which RFC 5545 treats as an ambiguous wall-clock time with no
real, fixed instant attached to it - not the unambiguous "this one thing
happens at this one specific moment" a REQUEST invitation is supposed to
describe. `build_ics` now takes the account's own timezone (`tz`, e.g.
"Europe/London" - the same setting already used for classification/School)
and converts a timezone-less start/end time into a real UTC instant
(correctly handling BST/GMT) before writing it out with a proper trailing
"Z". A start/end the AI *did* return with its own explicit offset is
trusted and converted to UTC as-is, never re-interpreted through the
account's timezone. Apple's exact rule for one-off-vs-subscribe still isn't
publicly documented anywhere reliable enough to say this alone fixes it,
which is why pipeline.py/web_app.py also added a plain-language landing
page in front of the raw .ics link this same round - see web_app.py's
`/ics/<token>` route.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


def _fmt(dt: datetime) -> str:
    """Formats a datetime that's already been normalised to UTC (see
    _to_utc) using RFC 5545's UTC form - the trailing "Z" is what makes this
    a real, unambiguous instant rather than the "floating" local time that
    was round 28's bug."""
    return dt.strftime("%Y%m%dT%H%M%SZ")


def _to_utc(dt: datetime, tz: str) -> datetime:
    """Normalises `dt` to a real UTC instant. If `dt` already carries its
    own offset (the AI occasionally includes one when the source email
    stated a timezone explicitly), that's trusted and just converted.
    Otherwise `dt` is naive/"floating" - it's localised into the account's
    own timezone (`tz`) before converting, so e.g. a 9am London event
    becomes the correct UTC instant whether it's during BST or GMT. Falls
    back to treating `dt` as UTC outright if `tz` isn't a real zone name,
    rather than losing the whole calendar link over it."""
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc)
    try:
        zone = ZoneInfo(tz)
    except Exception:  # noqa: BLE001 - unknown/invalid tz name; don't fail the event over it
        zone = timezone.utc
    return dt.replace(tzinfo=zone).astimezone(timezone.utc)


def build_ics(
    title: str,
    start_iso: str,
    end_iso: str = "",
    location: str = "",
    description: str = "",
    organizer_email: str = "",
    attendee_email: str = "",
    tz: str = "UTC",
) -> bytes | None:
    """Returns raw .ics bytes for a one-off event *invitation*, or None if
    start_iso couldn't be parsed (caller should just omit the Add to
    Calendar link in that case). `organizer_email`/`attendee_email`, when
    given, add real ORGANIZER/ATTENDEE properties and switch the calendar's
    METHOD to REQUEST - see the module docstring for why that's what
    actually makes this read as "one event to add", not a calendar to
    subscribe to. `tz` (an IANA zone name, e.g. "Europe/London") is only
    used when start_iso/end_iso don't already carry their own offset - see
    _to_utc."""
    try:
        start = datetime.fromisoformat(start_iso)
    except (ValueError, TypeError):
        return None
    start = _to_utc(start, tz)

    if end_iso:
        try:
            end = _to_utc(datetime.fromisoformat(end_iso), tz)
        except ValueError:
            end = start + timedelta(hours=1)
    else:
        end = start + timedelta(hours=1)

    uid = f"{uuid.uuid4()}@gmail-ai-sorter"
    now = _fmt(datetime.now(timezone.utc))
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
