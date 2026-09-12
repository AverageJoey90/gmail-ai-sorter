"""Builds the HTML digest email, matching the sections spec'd for this
project:

  [boxed counts: reviewed / sorted / left in inbox / needs reply]
  NEEDS A REPLY
  Good to know (top 5 important, with Add to Calendar links for events -
    falls back to a short "what happened" list when nothing was ranked
    important enough to include, so this section is never just empty)
  Sorted - label applied, archived out of Inbox
  Inbox - no good label match
"""
from __future__ import annotations

import html
from dataclasses import dataclass, field

from ics_builder import build_google_calendar_link

STYLE = """
body{font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;color:#1a1a1a;background:#fff;margin:0;padding:0}
.wrap{width:100%;background:#fff}

.header{background:#111f3d;padding:26px 32px;color:#fff}
.header .eyebrow{font-size:11px;letter-spacing:.08em;color:#9aa5c0;text-transform:uppercase;font-weight:700}
.header .row{display:flex;justify-content:space-between;align-items:baseline;margin-top:6px;flex-wrap:wrap;gap:6px}
.header .title{font-size:26px;font-weight:800}
.header .date{font-size:14px;color:#c7cfe3}

.content{padding:24px 32px 8px}

.counts{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:8px}
.count-box{flex:1 1 0;min-width:100px;border-radius:10px;padding:14px 14px;box-sizing:border-box}
.count-box .l{font-size:11px;text-transform:uppercase;letter-spacing:.05em;font-weight:700;margin-bottom:6px}
.count-box .n{font-size:26px;font-weight:800}
.count-box.reviewed{background:#f3f4f6}
.count-box.reviewed .l{color:#6b7280}
.count-box.reviewed .n{color:#111f3d}
.count-box.sorted{background:#eaf6ec}
.count-box.sorted .l{color:#2f7a45}
.count-box.sorted .n{color:#1f5c33}
.count-box.inbox{background:#fdf3e0}
.count-box.inbox .l{color:#a9740f}
.count-box.inbox .n{color:#8a5d0a}
.count-box.reply{background:#fbe9ea}
.count-box.reply .l{color:#b3404a}
.count-box.reply .n{color:#8f1f28}

h2{font-size:19px;font-weight:800;color:#111f3d;border-bottom:2px solid #111f3d;padding-bottom:10px;margin:30px 0 16px}

.needs-box{background:#fdf3e0;border-left:4px solid #d99a2b;border-radius:8px;padding:18px 20px;margin-bottom:14px}
.needs-box .hdr{color:#a9740f;font-weight:800;font-size:12px;letter-spacing:.04em;text-transform:uppercase;margin-bottom:12px}
.needs-box .subj{font-weight:700;font-size:16px;color:#1a1a1a;margin-bottom:8px}
.needs-box .summary{font-size:14px;color:#4b5563;line-height:1.55;margin-bottom:14px}

.goodbox{background:#f3f4f6;border-radius:8px;padding:14px 18px;margin-bottom:12px}
.goodbox .gtitle{font-weight:700;font-size:15px;color:#111f3d;margin-bottom:6px}
.goodbox .gbody{font-size:14px;color:#4b5563;line-height:1.55;margin-bottom:8px}

table.tbl{width:100%;border-collapse:collapse;background:#fff;margin-bottom:20px;border-radius:8px;overflow:hidden}
table.tbl thead tr{background:#111f3d}
table.tbl th{color:#fff;text-align:left;font-size:13px;padding:10px 14px;font-weight:700}
table.tbl td{padding:10px 14px;font-size:14px;border-bottom:1px solid #eee;color:#1a1a1a}
table.tbl tr:last-child td{border-bottom:none}
.pill{background:#e7e9f5;color:#2f3b7a;border-radius:20px;padding:3px 10px;font-size:12px;font-weight:700;white-space:nowrap}

a.btn-gold{display:inline-block;background:#d99a2b;color:#fff !important;text-decoration:none;font-weight:700;font-size:13px;padding:10px 18px;border-radius:6px}
a.btn-cal{display:inline-block;background:#111f3d;color:#fff !important;text-decoration:none;font-weight:700;font-size:13px;padding:8px 14px;border-radius:6px;margin-right:8px}
a.openlink{color:#2451c9;font-weight:700;font-size:13px;text-decoration:none}
a.link{color:#2451c9}
.empty{color:#888;font-style:italic;font-size:14px;margin-bottom:12px}

.footer{text-align:center;color:#9aa0ab;font-size:12px;padding:18px 32px 26px}
"""


@dataclass
class DigestData:
    account_address: str
    reviewed_count: int
    sorted_count: int
    left_in_inbox_count: int
    needs_reply_count: int
    needs_reply_items: list[dict] = field(default_factory=list)     # {subject, sender, summary, draft_link}
    top_important: list[dict] = field(default_factory=list)          # {subject, sender, summary, is_event, event_*, gmail_link}
    sorted_items: list[dict] = field(default_factory=list)           # {subject, sender, label, gmail_link}
    unmatched_items: list[dict] = field(default_factory=list)        # {subject, sender, reason, gmail_link}
    timezone: str = "Europe/London"
    date_label: str = ""


def _esc(s: str) -> str:
    return html.escape(s or "")


def _count_box(n: int, label: str, css_class: str) -> str:
    return (
        f'<div class="count-box {css_class}"><div class="l">{_esc(label)}</div>'
        f'<div class="n">{n}</div></div>'
    )


def _fallback_good_to_know(d: DigestData, limit: int = 5) -> list[dict]:
    """Used only when the AI's top-5 importance ranking came back empty
    (either it failed, or genuinely nothing stood out) - so "Good to know"
    never just says "nothing to report" while things clearly did happen.
    Surfaces a few of what was actually sorted/left behind instead, most
    recently-labelled first."""
    items: list[dict] = []
    for it in d.sorted_items:
        if len(items) >= limit:
            break
        items.append({
            "subject": it["subject"],
            "sender": it["sender"],
            "summary": f'Labelled "{it["label"]}" and archived out of the inbox.',
            "gmail_link": it.get("gmail_link"),
        })
    for it in d.unmatched_items:
        if len(items) >= limit:
            break
        items.append({
            "subject": it["subject"],
            "sender": it["sender"],
            "summary": it.get("reason") or "Left in the inbox - no confident label match.",
            "gmail_link": it.get("gmail_link"),
        })
    return items[:limit]


def build_digest_html(d: DigestData) -> str:
    parts = [f"<html><head><meta charset='utf-8'><style>{STYLE}</style></head><body><div class='wrap'>"]

    # Header
    parts.append('<div class="header"><div class="eyebrow">Inbox digest</div>')
    parts.append('<div class="row"><div class="title">Daily summary</div>')
    parts.append(f'<div class="date">{_esc(d.date_label)}</div></div></div>')

    parts.append('<div class="content">')

    # Boxed counts
    parts.append('<div class="counts">')
    parts.append(_count_box(d.reviewed_count, "Reviewed", "reviewed"))
    parts.append(_count_box(d.sorted_count, "Sorted", "sorted"))
    parts.append(_count_box(d.left_in_inbox_count, "Inbox", "inbox"))
    parts.append(_count_box(d.needs_reply_count, "Requires reply", "reply"))
    parts.append("</div>")

    # NEEDS A REPLY
    if d.needs_reply_items:
        for item in d.needs_reply_items:
            parts.append('<div class="needs-box"><div class="hdr">&#9888; Needs a reply</div>')
            parts.append(f'<div class="subj">{_esc(item["subject"])}</div>')
            parts.append(f'<div class="summary">{_esc(item.get("summary", ""))}</div>')
            if item.get("draft_link"):
                parts.append(f'<a class="btn-gold" href="{_esc(item["draft_link"])}">View draft in Gmail &rarr;</a>')
            else:
                parts.append('<span class="empty">Draft could not be created - see logs.</span>')
            parts.append("</div>")

    # Good to know / top 5 (falls back to sorted/unmatched highlights if empty)
    parts.append("<h2>Good to know</h2>")
    good_items = d.top_important or _fallback_good_to_know(d)
    if not good_items:
        parts.append('<div class="empty">Nothing new to flag today.</div>')
    for idx, item in enumerate(good_items, start=1):
        parts.append('<div class="goodbox">')
        parts.append(f'<div class="gtitle">{idx}. {_esc(item["subject"])}</div>')
        parts.append(f'<div class="gbody">{_esc(item.get("summary", ""))}</div>')
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
                parts.append(f'<a class="btn-cal" href="{_esc(cal_link)}">Add to Calendar &rarr;</a>')
        if item.get("gmail_link"):
            parts.append(f'<a class="openlink" href="{_esc(item["gmail_link"])}">Open &rarr;</a>')
        parts.append("</div>")

    # Sorted
    parts.append("<h2>Sorted &mdash; label applied, archived out of Inbox</h2>")
    if not d.sorted_items:
        parts.append('<div class="empty">Nothing matched a label well enough to sort today.</div>')
    else:
        parts.append('<table class="tbl"><thead><tr><th>Email</th><th>Sender</th><th>Label applied</th></tr></thead><tbody>')
        for item in d.sorted_items:
            sender = _esc(item["sender"])
            parts.append(
                "<tr>"
                f'<td>{_esc(item["subject"])}</td>'
                f'<td><a class="link" href="mailto:{sender}">{sender}</a></td>'
                f'<td><span class="pill">{_esc(item["label"])}</span></td>'
                "</tr>"
            )
        parts.append("</tbody></table>")

    # Inbox - no good label match
    parts.append("<h2>Inbox &mdash; no good label match</h2>")
    if not d.unmatched_items:
        parts.append('<div class="empty">Everything in the inbox was sorted.</div>')
    else:
        parts.append('<table class="tbl"><thead><tr><th>Email</th><th>Summary</th></tr></thead><tbody>')
        for item in d.unmatched_items:
            parts.append(
                "<tr>"
                f'<td>{_esc(item["subject"])}</td>'
                f'<td>{_esc(item.get("reason", "No confident label match."))}</td>'
                "</tr>"
            )
        parts.append("</tbody></table>")

    parts.append("</div>")  # .content
    parts.append('<div class="footer">Automated daily digest of your Gmail inbox sort. No action needed unless flagged above.</div>')
    parts.append("</div></body></html>")
    return "".join(parts)
