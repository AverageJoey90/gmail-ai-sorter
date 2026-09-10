"""Builds "Add to Calendar" links for the digest.

Primary mechanism: a plain https://calendar.google.com/calendar/render
link. This is what Litmus/major ESPs recommend specifically because it is
an ordinary hyperlink - no server hosting, no port-forwarding/DDNS needed
on the NAS, and critically (unlike a data:text/calendar link) it is NOT
stripped by Gmail's HTML sanitizer, which does strip data: URIs from
received email links. Since this whole project lives inside Gmail, that
makes it the reliable choice here.

A raw .ics builder is also kept below for anyone who later wants a
non-Google-Calendar fallback (e.g. to attach a file instead) - it's just
not wired into the digest by default because a data: link version of it
would silently fail to open in Gmail.
"""
from __future__ import annotations

import base64
import uuid
from datetime import datetime, timedelta
from urllib.parse import urlencode


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


def ics_data_uri(ics_bytes: bytes) -> str:
    b64 = base64.b64encode(ics_bytes).decode("ascii")
    return f"data:text/calendar;charset=utf-8;base64,{b64}"


def build_google_calendar_link(
    title: str,
    start_iso: str,
    end_iso: str = "",
    location: str = "",
    description: str = "",
    tz: str = "Europe/London",
) -> str | None:
    """Returns a calendar.google.com/render URL that opens Google Calendar
    with the event pre-filled, ready for one click to save - or None if
    start_iso couldn't be parsed (caller should omit the link entirely).

    Dates are sent as floating local time (no Z/UTC suffix) plus a `ctz`
    parameter, so no UTC conversion is needed here - Google Calendar
    interprets the given YYYYMMDDTHHMMSS in that timezone.
    """
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

    # Strip any timezone info the model may have included - we're treating
    # these as floating local times qualified by the separate ctz param.
    start = start.replace(tzinfo=None)
    end = end.replace(tzinfo=None)

    params = {
        "action": "TEMPLATE",
        "text": title,
        "dates": f"{_fmt(start)}/{_fmt(end)}",
        "ctz": tz,
    }
    if location:
        params["location"] = location
    if description:
        params["details"] = description

    return "https://calendar.google.com/calendar/render?" + urlencode(params)
