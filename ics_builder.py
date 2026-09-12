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
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y%m%dT%H%M%S")


def build_ics(title: str, start_iso: str, end_iso: str = "", location: str = "", description: str = "") -> bytes | None:
    """Returns raw .ics bytes, or None if start_iso couldn't be parsed
    (caller should just omit the Add to Calendar link in that case)."""
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

    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;").replace("\n", "\\n")

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//gmail-ai-sorter//EN",
        "CALSCALE:GREGORIAN",
        # Without an explicit METHOD, some calendar apps (notably iOS) treat
        # a bare .ics resource as ambiguous and offer to "Subscribe" to it as
        # a live, ongoing feed rather than import it as a one-off event.
        # METHOD:PUBLISH is the RFC 5545 way of saying "this is a single
        # snapshot of an event, add it" - it's what actually produces the
        # native one-time "Add to Calendar"/"Add Event" sheet instead of a
        # subscription prompt.
        "METHOD:PUBLISH",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{now}",
        f"DTSTART:{_fmt(start)}",
        f"DTEND:{_fmt(end)}",
        f"SUMMARY:{esc(title)}",
    ]
    if location:
        lines.append(f"LOCATION:{esc(location)}")
    if description:
        lines.append(f"DESCRIPTION:{esc(description)}")
    lines += ["END:VEVENT", "END:VCALENDAR"]

    # CRLF per RFC 5545.
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")
