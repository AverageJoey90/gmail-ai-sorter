"""Builds the HTML digest email, matching the sections spec'd for this
project:

  [boxed counts: reviewed / sorted / left in inbox / needs reply]
  NEEDS A REPLY
  Good to know (top 5 important, with Add to Calendar links for events)
  Sorted - label applied, archived out of Inbox
  Inbox - no good label match
"""
from __future__ import annotations

import html
from dataclasses import dataclass, field

from ics_builder import build_google_calendar_link

STYLE = """
body{font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;color:#1a1a1a;background:#f5f5f5;margin:0;padding:16px}
.wrap{max-width:640px;margin:0 auto}
.counts{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:20px}
.count-box{flex:1;min-width:120px;background:#fff;border:1px solid #ddd;border-radius:8px;padding:12px;text-align:center}
.count-box .n{font-size:24px;font-weight:700;color:#1a56db}
.count-box .l{font-size:12px;color:#555;text-transform:uppercase;letter-spacing:.03em}
h2{font-size:15px;text-transform:uppercase;letter-spacing:.04em;color:#333;border-bottom:2px solid #1a56db;padding-bottom:6px;margin-top:28px}
.item{background:#fff;border:1px solid #e5e5e5;border-radius:8px;padding:12px 14px;margin-bottom:10px}
.item .subj{font-weight:600}
.item .meta{font-size:12px;color:#666;margin-bottom:6px}
.item .body{font-size:14px;color:#333}
a.btn{display:inline-block;margin-top:8px;padding:6px 12px;background:#1a56db;color:#fff !important;text-decoration:none;border-radius:6px;font-size:13px}
a.link{color:#1a56db}
.empty{color:#888;font-style:italic;font-size:14px}
"""


@dataclass
class DigestData:
    account_address: str
    reviewed_count: int
    sorted_count: int
    left_in_inbox_count: int
    needs_reply_count: int
    needs_reply_items: list[dict] = field(default_factory=list)     # {subject, sender, summary, draft_link}
    top_important: list[dict] = field(default_factory=list)          # {subject, sender, summary, is_event, event_*}
    sorted_items: list[dict] = field(default_factory=list)           # {subject, sender, label}
    unmatched_items: list[dict] = field(default_factory=list)        # {subject, sender, reason}
    timezone: str = "Europe/London"


def _esc(s: str) -> str:
    return html.escape(s or "")


def _count_box(n: int, label: str) -> str:
    return f'<div class="count-box"><div class="n">{n}</div><div class="l">{_esc(label)}</div></div>'


def _item(subject: str, sender: str, body_html: str) -> str:
    return (
        f'<div class="item"><div class="subj">{_esc(subject)}</div>'
        f'<div class="meta">{_esc(sender)}</div>'
        f'<div class="body">{body_html}</div></div>'
    )


def build_digest_html(d: DigestData) -> str:
    parts = [f"<html><head><meta charset='utf-8'><style>{STYLE}</style></head><body><div class='wrap'>"]

    parts.append('<div class="counts">')
    parts.append(_count_box(d.reviewed_count, "Reviewed"))
    parts.append(_count_box(d.sorted_count, "Sorted"))
    parts.append(_count_box(d.left_in_inbox_count, "Left in inbox"))
    parts.append(_count_box(d.needs_reply_count, "Needs reply"))
    parts.append("</div>")

    # NEEDS A REPLY
    parts.append("<h2>Needs a reply</h2>")
    if not d.needs_reply_items:
        parts.append('<div class="empty">Nothing needs a reply today.</div>')
    for item in d.needs_reply_items:
        body = _esc(item.get("summary", ""))
        if item.get("draft_link"):
            body += f'<br><a class="btn" href="{_esc(item["draft_link"])}">Open draft in Gmail</a>'
        else:
            body += '<br><span class="empty">Draft could not be created - see logs.</span>'
        parts.append(_item(item["subject"], item["sender"], body))

    # Good to know / top 5
    parts.append("<h2>Good to know</h2>")
    if not d.top_important:
        parts.append('<div class="empty">Nothing stood out as a top pick today.</div>')
    for item in d.top_important:
        body = _esc(item.get("summary", ""))
        if item.get("is_event") and item.get("event_start"):
            cal_link = build_google_calendar_link(
                title=item.get("event_title") or item["subject"],
                start_iso=item["event_start"],
                end_iso=item.get("event_end", ""),
                location=item.get("event_location", ""),
                description=item.get("summary", ""),
                tz=d.timezone,
            )
            if cal_link:
                body += f'<br><a class="btn" href="{_esc(cal_link)}">Add to Calendar</a>'
        parts.append(_item(item["subject"], item["sender"], body))

    # Sorted
    parts.append("<h2>Sorted &mdash; label applied, archived out of Inbox</h2>")
    if not d.sorted_items:
        parts.append('<div class="empty">Nothing matched a label well enough to sort today.</div>')
    for item in d.sorted_items:
        body = f'Labelled <strong>{_esc(item["label"])}</strong>'
        parts.append(_item(item["subject"], item["sender"], body))

    # Inbox - no good label match
    parts.append("<h2>Inbox &mdash; no good label match</h2>")
    if not d.unmatched_items:
        parts.append('<div class="empty">Everything in the inbox was sorted.</div>')
    for item in d.unmatched_items:
        body = _esc(item.get("reason", "No confident label match."))
        parts.append(_item(item["subject"], item["sender"], body))

    parts.append("</div></body></html>")
    return "".join(parts)
