"""The actual sort + digest run for one Gmail account. Used by both the
daily background scheduler and the dashboard's "Run now" button, so there
is exactly one implementation of the logic described in the project spec.
"""
from __future__ import annotations

import html
import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from ai_client import AiClient
from config import BootstrapConfig
from digest_builder import DigestData, build_digest_html
from gmail_client import GmailClient
from ics_builder import build_ics
from ics_store import IcsStore
from oauth_web import token_path_for
from settings_store import DEFAULT_DIGEST_FREQUENCY, SettingsStore

log = logging.getLogger(__name__)

# How many days an unread inbox message is protected from labelling/archiving
# when an account has "hold_unread_emails" turned on (Joe: 'leaves unread
# emails for a maximum of 7 days before applying a label'). It's still fully
# processed otherwise - classified, eligible for Good to know/School, and
# still gets a draft reply if needs_reply - only the label-apply-and-archive
# step itself is skipped while it's within its grace period.
HOLD_UNREAD_GRACE_DAYS = 7

# Joe: "if you find an old Daily or Weekly digest then move that into the
# Weekly Digest folder/label to be replaced by the newest one" - a global
# (not per-account) opt-in that tidies up this app's own past digest emails
# instead of leaving them to pile up in the inbox (or get accidentally
# swept up and re-sorted by this run's own inbox-sort step, if the digest
# is sent back to the same account it came from). Every past digest that's
# found gets archived under this one label - none are ever removed, so it
# builds a running history rather than trying to keep just the latest.
# The label's actual name/path is a configurable setting (see
# settings_store.py's "weekly_digest_label_name") since Gmail's nested
# labels use their full path as their real name (Joe's own real one turned
# out to be "INBOX/Weekly Digest", not just "Weekly Digest") - this
# constant is only the seed default for a fresh settings.json.
DEFAULT_WEEKLY_DIGEST_LABEL_NAME = "Weekly Digest"
# The actual subject line sent, with {frequency} filled in from the
# account's own daily/weekly setting (Joe: the subject always said "Gmail
# daily digest" even for a weekly-configured account - a real bug, now
# fixed - see run_account below).
DIGEST_SUBJECT_PREFIX_TEMPLATE = "Gmail {frequency} digest - {address} - "
# Used only to SEARCH for past digests (_archive_old_digests) - deliberately
# frequency-agnostic (no "daily"/"weekly" word) so it still finds every past
# digest regardless of which wording was used at the time: every digest
# this app has ever sent (including every one sent before the subject-line
# fix above existed, which all said "daily" unconditionally) contains this
# exact substring.
DIGEST_SUBJECT_SEARCH_ANCHOR = "digest - {address} - "

# How many emails go into one classify_emails_batch() Gemini call instead
# of one call each - see ai_client.py. Kept modest rather than as large as
# possible: a bigger batch means fewer calls, but also means a single
# failed/malformed batch call leaves more messages without a result (each
# one still falls through to the same "left in inbox"/"classification
# failed" handling a lone failure would, just more of them at once) - 5
# was picked as a reasonable balance, not a hard requirement.
CLASSIFY_BATCH_SIZE = 5


def _chunk(items: list, size: int) -> list[list]:
    return [items[i:i + size] for i in range(0, len(items), size)]


def resolve_gemini_api_key(account: dict, bootstrap: BootstrapConfig) -> str:
    """An account can optionally carry its own Gemini API key (see
    settings_store.py's "gemini_api_key") so two accounts sharing this app
    aren't sharing one API key's rate limit either - falls back to the
    shared key from Setup when the account hasn't set one, which is the
    only behaviour that existed before this option and stays the default
    for every account unless someone opts in."""
    return account.get("gemini_api_key") or bootstrap.gemini_api_key


def _message_age_days(internal_date_ms: int, now: datetime | None = None) -> float:
    """How many days old a message is, based on Gmail's internalDate (epoch
    ms). Takes an explicit `now` so this stays deterministically testable
    without mocking the clock. A missing/zero internal_date_ms (e.g. an old
    test fake that doesn't set it) is treated as "infinitely old" - i.e. NOT
    held - so this feature only ever holds messages it can actually confirm
    are recent, never blocks labelling due to missing data."""
    if not internal_date_ms:
        return float("inf")
    now = now or datetime.now(ZoneInfo("UTC"))
    received = datetime.fromtimestamp(internal_date_ms / 1000, tz=ZoneInfo("UTC"))
    return (now - received).total_seconds() / 86400.0


_FORWARD_MARKER_RE = re.compile(r'^[-_]{2,}\s*(forwarded message|original message)\s*[-_]*$', re.IGNORECASE)
_REPLY_QUOTE_RE = re.compile(r'^on .{0,120}wrote:\s*$', re.IGNORECASE)
_HEADER_LINE_RE = re.compile(r'^(from|to|cc|bcc|date|sent|subject|reply-to):', re.IGNORECASE)


def _clean_excerpt(text: str, max_chars: int = 200) -> str:
    """Strips forwarded-message/reply-quote boilerplate (the "----------
    Forwarded message ---------" block Gmail inserts, its From/Date/
    Subject/To header lines, and "On <date>, <person> wrote:" reply
    quotes) before taking a short plain-text excerpt. Used anywhere a raw
    Gmail snippet/body would otherwise stand in for a real summary, so a
    forwarded school email's quoted header block (sender name, timestamp,
    raw email address) never ends up shown as if it were one (Joe: "just
    a small summary only not all this junk like date time, email
    addresses"). `html.unescape` runs first because a forwarded
    plain-text body sometimes carries literal HTML entities (e.g.
    "&lt;name@example.com&gt;") baked in by whatever mail client built
    the forward. Returns "" if nothing meaningful is left once the
    boilerplate is stripped, so the caller can fall back to something
    else rather than show an empty/junk box."""
    text = html.unescape(text or "")
    lines = text.splitlines()
    start = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if _FORWARD_MARKER_RE.match(stripped) or _REPLY_QUOTE_RE.match(stripped):
            start = i + 1
            while start < len(lines) and (not lines[start].strip() or _HEADER_LINE_RE.match(lines[start].strip())):
                start += 1
            break
    remainder = " ".join(" ".join(lines[start:]).split())
    if not remainder:
        return ""
    if len(remainder) <= max_chars:
        return remainder
    cut = remainder[:max_chars].rsplit(" ", 1)[0]
    return (cut or remainder[:max_chars]).rstrip(",.;: ") + "…"


def _build_calendar_link(
    item: dict, ics_store: IcsStore, public_base_url: str, organizer_email: str, attendee_email: str,
) -> str | None:
    """If `item` (an AI-ranked "Good to know"/School entry) describes a
    dated event, builds its .ics file (as a real invitation - see
    ics_builder.py - organized by this Gmail account, addressed to whoever
    the digest itself was sent to), saves it via `ics_store`, and returns a
    link to the dashboard's `/ics/<token>.ics` route that serves it -
    tapping that link is what triggers a native "Add to Calendar" prompt on
    the device (iPhone included), since it's a real .ics file with a
    text/calendar content type rather than a Google-Calendar-only web link.
    Returns None (no calendar link shown) if there's no event, or its start
    time couldn't be parsed."""
    if not item.get("is_event") or not item.get("event_start"):
        return None
    ics_bytes = build_ics(
        title=item.get("event_title") or item.get("subject", ""),
        start_iso=item["event_start"],
        end_iso=item.get("event_end", ""),
        location=item.get("event_location", ""),
        description=item.get("summary", ""),
        organizer_email=organizer_email,
        attendee_email=attendee_email,
    )
    if not ics_bytes:
        return None
    token = ics_store.save_event(ics_bytes)
    return f"{public_base_url.rstrip('/')}/ics/{token}.ics"


def _archive_old_digests(gmail: GmailClient, address: str, label_name: str) -> int:
    """Moves any of THIS account's own previously-sent digest emails that
    are still sitting in its inbox into `label_name` (matched case-
    insensitively against this account's real labels, including a nested
    one's full path - see GmailClient.get_or_create_label - and created if
    truly nothing matches), so they don't clutter the inbox and - just as
    importantly - aren't accidentally picked up and re-processed by this
    same run's own inbox-sort step as if they were new mail (relevant
    whenever the digest is sent back to the same account it came from).
    Anything already archived/labelled from a previous run is left alone
    (this only ever looks at what's currently in the inbox). Returns how
    many were moved, for logging only."""
    try:
        label_id = gmail.get_or_create_label(label_name)
    except Exception:
        log.exception("%s: failed to get/create the '%s' label, skipping digest cleanup this run", address, label_name)
        return 0
    anchor = DIGEST_SUBJECT_SEARCH_ANCHOR.format(address=address)
    try:
        ids = gmail.search_message_ids(f'in:inbox subject:"{anchor}"', max_results=50)
    except Exception:
        log.exception("%s: failed searching for old digest emails, skipping cleanup this run", address)
        return 0
    moved = 0
    for msg_id in ids:
        try:
            gmail.apply_label_and_archive(msg_id, label_id)
            moved += 1
        except Exception:
            log.exception("%s: failed to move old digest message %s into '%s'", address, msg_id, label_name)
    return moved


def run_account(account: dict, bootstrap: BootstrapConfig, settings: dict, ai: AiClient) -> dict:
    """Runs one full sort+digest cycle for `account` (a record from
    SettingsStore.list_accounts()). Returns a small summary dict suitable
    for SettingsStore.record_run_result and for display in the dashboard."""
    address = account["address"]
    index = account["index"]
    recipient = account.get("digest_recipient") or address
    log.info("Starting run for %s", address)

    token_file = token_path_for(bootstrap, index)
    gmail = GmailClient(token_file)
    ics_store = IcsStore(bootstrap.data_dir)

    # Joe's "move old digests into a Weekly Digest folder" setting - global,
    # so it's read straight off `settings` rather than the account record.
    # Deliberately done before anything else in this run (before even the
    # inbox sort below): if this digest gets sent back to this same
    # account, last run's copy would otherwise still be sitting unlabelled
    # in the inbox right now and get swept up as if it were brand-new mail.
    if bool(settings.get("move_old_digests_to_weekly_folder")):
        weekly_digest_label_name = settings.get("weekly_digest_label_name") or DEFAULT_WEEKLY_DIGEST_LABEL_NAME
        moved = _archive_old_digests(gmail, address, weekly_digest_label_name)
        if moved:
            log.info("%s: moved %d old digest email(s) into '%s'", address, moved, weekly_digest_label_name)

    ignore_labels = set(settings.get("ignore_labels", []))
    threshold = float(settings.get("classify_confidence_threshold", 0.7))
    tz = settings.get("timezone", "Europe/London")
    # Labels that get their own dedicated "School" digest section instead of
    # competing for a slot in the general "Good to know" ranking - see the
    # independent School-section step further down. Case-insensitive.
    #
    # Deliberately SUBSTRING matching, not exact-name matching: Gmail returns
    # nested labels as their full path (e.g. "Family/School"), and people
    # often word a label as "School - Yeomoor Wood" or similar rather than
    # a bare "School" - none of those equal "school" outright, but all of
    # them contain it. So each configured keyword (default just "school")
    # is checked as a substring of the label's full lowercased name/path,
    # anywhere in it, rather than requiring the whole name to match.
    school_keywords = [s.strip().lower() for s in settings.get("school_section_labels", []) if s.strip()]

    def _is_school_label(name: str) -> bool:
        lowered = name.strip().lower()
        return any(kw in lowered for kw in school_keywords)
    # "daily" (this account's own setting - see settings_store.py) means the
    # labelled-folder sweep below only looks at unread mail, same as always;
    # "weekly" broadens that to everything from the last 7 days regardless
    # of read state, since a weekly account isn't checked in between.
    # `or DEFAULT_DIGEST_FREQUENCY` is only a defensive fallback for an
    # account record saved by a pre-round-18 version of this app.
    frequency = account.get("digest_frequency") or DEFAULT_DIGEST_FREQUENCY
    # Joe: 'leave unread emails in inbox for 7 days before labelling ... you
    # still need to read them to allow them to appear in the good to know
    # and school sections and make a draft'. Purely per-account, off by
    # default (see settings_store.add_account).
    hold_unread_emails = bool(account.get("hold_unread_emails"))

    label_map = gmail.list_user_labels(ignore=ignore_labels)
    label_names = list(label_map.keys())
    log.info("%s: %d existing labels available to sort into", address, len(label_names))
    # Resolved once so every classified/found message can be cheaply checked
    # against it, and so the School-section step below knows which actual
    # label names in this account it should search.
    school_label_ids = {label_map[name] for name in label_names if _is_school_label(name)}
    school_label_names = [name for name in label_names if _is_school_label(name)]

    needs_reply_items: list[dict] = []
    sorted_items: list[dict] = []
    unmatched_items: list[dict] = []
    all_reviewed: dict[str, dict] = {}  # ref -> {subject, sender, body, gmail_link, exclude_from_good_to_know}
    # Joe: "if something appears once across the needs a reply, good to know
    # and school it is only displayed once across all three, the priority
    # should be for potential replies then everything else can be populated
    # below" - refs of every message that actually lands in "Needs a reply"
    # (decided here, before Good to know/School are built further down), so
    # those two sections can exclude them and never show the same email
    # twice. School vs. Good to know was already mutually exclusive before
    # this (via exclude_from_good_to_know) - this closes the remaining gap.
    needs_reply_refs: set[str] = set()

    def maybe_flag_needs_reply(ref: str, msg, ai_result: dict) -> None:
        if not ai_result.get("needs_reply"):
            return
        needs_reply_refs.add(ref)
        draft_link = None
        reply_gist = ""
        existing = gmail.has_existing_draft_for_thread(msg.thread_id)
        if existing:
            draft_link = GmailClient.draft_permalink(existing)
            # No fresh draft_reply call was made (and none needed - a draft
            # already exists), so there's no generated gist to show; say so
            # plainly rather than guessing at what an existing draft says.
            reply_gist = "A draft reply already exists on this thread - open it in Gmail to review."
        else:
            try:
                drafted = ai.draft_reply(msg.subject, msg.sender, msg.body_text)
                draft_msg_id = gmail.create_draft_reply(msg, drafted["reply_body"], address)
                draft_link = GmailClient.draft_permalink(draft_msg_id)
                reply_gist = drafted.get("reply_gist", "")
            except Exception:
                log.exception("%s: failed to create draft for message %s", address, msg.id)
        needs_reply_items.append({
            "ref": ref,
            "subject": msg.subject,
            "sender": msg.sender,
            "summary": ai_result.get("summary") or _clean_excerpt(msg.body_text) or "Open in Gmail to see the full message.",
            "reply_gist": reply_gist,
            "draft_link": draft_link,
        })

    # ---- 1. Inbox sort -----------------------------------------------------------
    inbox_ids = gmail.search_message_ids("in:inbox", max_results=200)
    log.info("%s: %d messages currently in inbox", address, len(inbox_ids))
    inbox_msgs = []
    for msg_id in inbox_ids:
        try:
            msg = gmail.get_message(msg_id)
        except Exception:
            log.exception("%s: failed to fetch inbox message %s, skipping", address, msg_id)
            continue

        ref = f"inbox_{msg.id}"
        all_reviewed[ref] = {
            "ref": ref, "subject": msg.subject, "sender": msg.sender,
            "body": msg.body_text, "gmail_link": msg.permalink(),
            # A brand-new inbox message can't already carry a School label,
            # but check anyway for consistency with the folder-sweep loop;
            # updated below if it gets confidently labeled School this run.
            "exclude_from_good_to_know": bool(set(msg.label_ids) & school_label_ids),
        }
        inbox_msgs.append((ref, msg))

    # Classified in batches (see CLASSIFY_BATCH_SIZE) rather than one
    # Gemini call per message - each email is still judged completely on
    # its own (see the batch prompt in ai_client.py), this just cuts the
    # number of API calls a busy inbox makes. A ref missing from the
    # results (its whole batch call failed, or the model skipped it) is
    # handled the same way a single classify_email failure always was.
    inbox_results: dict[str, dict] = {}
    for batch in _chunk(inbox_msgs, CLASSIFY_BATCH_SIZE):
        batch_payload = [
            {"ref": ref, "subject": msg.subject, "sender": msg.sender, "body": msg.body_text}
            for ref, msg in batch
        ]
        try:
            inbox_results.update(ai.classify_emails_batch(batch_payload, label_names))
        except Exception:
            log.exception("%s: batch classification failed for %d inbox message(s)", address, len(batch))

    for ref, msg in inbox_msgs:
        msg_id = msg.id
        result = inbox_results.get(ref)
        if result is None:
            log.warning("%s: no classification result for inbox message %s, leaving in inbox", address, msg_id)
            unmatched_items.append({
                "subject": msg.subject, "sender": msg.sender,
                # No AI result at all here (the whole batch call failed), so
                # there's no AI-written summary to use - fall back to a
                # clean excerpt of the email itself rather than showing a
                # bare technical error where a summary should be (Joe: "I
                # am seeing a lot of 'Classification failed'... where
                # simple summaries should be"). "reason" is kept too, for
                # the container logs/troubleshooting - it's just not what's
                # shown as the digest's "Summary" column any more.
                "summary": _clean_excerpt(msg.body_text) or "Open in Gmail to see the full message.",
                "reason": "Classification failed - see container logs.", "gmail_link": msg.permalink(),
            })
            continue

        is_unread = "UNREAD" in msg.label_ids
        should_hold = (
            hold_unread_emails
            and is_unread
            and result["label"] and result["confidence"] >= threshold
            and _message_age_days(msg.internal_date_ms) < HOLD_UNREAD_GRACE_DAYS
        )

        if should_hold:
            # Confidently matched a label, but it's still unread and within
            # its grace period - leave it alone in the inbox (no label, no
            # archive) even though a match was found. It's already been
            # fully processed above (all_reviewed / Good to know / School
            # eligibility, and maybe_flag_needs_reply below still runs), so
            # nothing about that is skipped - only the label-apply step is.
            held_reason = (
                f"Unread and within its {HOLD_UNREAD_GRACE_DAYS}-day grace period - "
                f"would be labelled \"{result['label']}\" once read or after {HOLD_UNREAD_GRACE_DAYS} days."
            )
            unmatched_items.append({
                "subject": msg.subject, "sender": msg.sender,
                # This explanatory note IS the most useful thing to show
                # here (Joe confirmed this in round 20) - shown as-is,
                # unlike the two failure cases below which get a real
                # content summary instead of their technical reason text.
                "summary": held_reason, "reason": held_reason,
                "gmail_link": msg.permalink(),
            })
            try:
                # Defensive only: nothing in this run should have marked the
                # message read (get_message is a plain GET, and the label
                # apply step that would remove UNREAD's sibling INBOX is
                # exactly the step being skipped) - but if anything ever
                # changes that, this guarantees the message stays/becomes
                # unread again rather than silently losing that status.
                gmail.mark_unread(msg.id)
            except Exception:
                log.exception("%s: failed to re-mark %s as unread", address, msg_id)
        elif result["label"] and result["confidence"] >= threshold:
            try:
                gmail.apply_label_and_archive(msg.id, label_map[result["label"]])
                sorted_items.append({
                    "subject": msg.subject, "sender": msg.sender,
                    "label": result["label"], "gmail_link": msg.permalink(),
                })
                if _is_school_label(result["label"]):
                    # Now actually carries the School label (just applied
                    # above) - the dedicated School section's own search
                    # will find it, so keep it out of Good to know too.
                    all_reviewed[ref]["exclude_from_good_to_know"] = True
            except Exception:
                log.exception("%s: failed to apply label to %s", address, msg_id)
                unmatched_items.append({
                    "subject": msg.subject, "sender": msg.sender,
                    # Classification itself succeeded here (only the Gmail
                    # label-apply call failed), so the AI's own summary is
                    # available - prefer it, same reasoning as the
                    # classification-failure case above.
                    "summary": result.get("summary") or _clean_excerpt(msg.body_text) or "Open in Gmail to see the full message.",
                    "reason": "Label apply failed - see container logs.", "gmail_link": msg.permalink(),
                })
        else:
            reason = result.get("reasoning") or "No existing label was a confident match."
            unmatched_items.append({
                "subject": msg.subject, "sender": msg.sender,
                # Prefer the AI's own plain-English summary of the email
                # over its labelling `reasoning` here - "reasoning" explains
                # the labelling decision (e.g. "doesn't match any existing
                # label pattern"), which isn't a summary of the email
                # itself and reads oddly in a "Summary" column.
                "summary": result.get("summary") or _clean_excerpt(msg.body_text) or reason,
                "reason": reason, "gmail_link": msg.permalink(),
            })

        maybe_flag_needs_reply(ref, msg, result)

    # ---- 2. Sweep other labelled folders for new mail -----------------------------------------------------------
    # Daily accounts only need unread mail here (this same folder was fully
    # swept as of the last run); weekly accounts aren't checked in between,
    # so they look at everything from the last 7 days regardless of
    # read/unread state, per Joe's explicit requirement for weekly mode.
    folder_query_suffix = "newer_than:7d" if frequency == "weekly" else "is:unread"
    folder_ids: set[str] = set()
    school_folder_ids: set[str] = set()  # ids found specifically via a School label, excluded from Good to know
    for name in label_names:
        try:
            ids = gmail.search_message_ids(f'label:"{name}" {folder_query_suffix}', max_results=100)
        except Exception:
            log.exception("%s: failed searching label %s, skipping", address, name)
            continue
        folder_ids.update(ids)
        if _is_school_label(name):
            school_folder_ids.update(ids)

    log.info("%s: %d messages found across %d labelled folders (%s)", address, len(folder_ids), len(label_names), folder_query_suffix)
    folder_msgs = []
    for msg_id in folder_ids:
        try:
            msg = gmail.get_message(msg_id)
        except Exception:
            log.exception("%s: failed fetching folder message %s, skipping", address, msg_id)
            continue

        ref = f"folder_{msg.id}"
        all_reviewed[ref] = {
            "ref": ref, "subject": msg.subject, "sender": msg.sender,
            "body": msg.body_text, "gmail_link": msg.permalink(),
            "exclude_from_good_to_know": msg_id in school_folder_ids or bool(set(msg.label_ids) & school_label_ids),
        }
        folder_msgs.append((ref, msg))

    # Batched the same way as the inbox sort above (see CLASSIFY_BATCH_SIZE).
    folder_results: dict[str, dict] = {}
    for batch in _chunk(folder_msgs, CLASSIFY_BATCH_SIZE):
        batch_payload = [
            {"ref": ref, "subject": msg.subject, "sender": msg.sender, "body": msg.body_text}
            for ref, msg in batch
        ]
        try:
            folder_results.update(ai.classify_emails_batch(batch_payload, label_names))
        except Exception:
            log.exception("%s: batch classification failed for %d folder message(s)", address, len(batch))

    for ref, msg in folder_msgs:
        result = folder_results.get(ref)
        if result is None:
            # Deliberately NOT marking this one read: it hasn't actually
            # been checked for needs-reply yet, so it needs to still show
            # up in a future is:unread sweep rather than silently losing
            # its needs-reply check forever the moment classification
            # fails once (the bug an earlier version of this file had).
            log.warning(
                "%s: needs-reply check skipped for folder message %s (no classification result) - "
                "left unread, will be retried next sweep", address, msg.id,
            )
            continue
        try:
            gmail.mark_read(msg.id)
        except Exception:
            log.exception("%s: failed to mark folder message %s as read", address, msg.id)
        maybe_flag_needs_reply(ref, msg, result)

    # ---- 3. Top 5 important + event detection ("Good to know") -----------------------------------------------------------
    # School-section candidates are left out of this ranking pool entirely -
    # they get their own dedicated section below, so there's no duplication
    # between the two and no risk of the general ranking outranking them
    # into a worse/duplicate slot. Anything already shown in "Needs a
    # reply" is left out too - Joe wants each email shown once across all
    # three sections, with Needs a reply taking priority since it's already
    # been decided by this point in the run.
    good_to_know_pool = [
        v for v in all_reviewed.values()
        if not v.get("exclude_from_good_to_know") and v["ref"] not in needs_reply_refs
    ]
    top_picks = []
    if good_to_know_pool:
        try:
            ranked = ai.rank_and_summarize(good_to_know_pool, top_n=5, tz=tz)
            for r in ranked:
                base = all_reviewed.get(r["ref"])
                if not base:
                    continue
                item = {**r, "subject": base["subject"], "sender": base["sender"], "gmail_link": base.get("gmail_link")}
                item["calendar_link"] = _build_calendar_link(item, ics_store, bootstrap.public_base_url, address, recipient)
                top_picks.append(item)
        except Exception:
            log.exception("%s: importance ranking failed, omitting 'Good to know' section", address)

    # ---- 3b. School section: independent recent-window search, top 3 -----------------------------------------------------------
    # Deliberately its own search (not just reusing what the sweep above
    # happened to find), and deliberately NOT tied to the daily/weekly
    # frequency above - the School section is a "what's coming up" reminder
    # (Joe's framing), so its lookback is its own configurable setting
    # rather than shrinking to match a weekly account's shorter sweep
    # window. Messages fetched only for this step are NOT added to
    # all_reviewed / marked read / counted as "reviewed": surfacing
    # something in a digest highlight isn't the same as having processed it.
    school_items: list[dict] = []
    if school_label_names:
        try:
            school_lookback_days = max(1, int(settings.get("school_lookback_days", 14) or 14))
        except (TypeError, ValueError):
            school_lookback_days = 14
        school_window = f"newer_than:{school_lookback_days}d"
        candidate_ids: list[str] = []
        seen_ids: set[str] = set()
        for name in school_label_names:
            try:
                ids = gmail.search_message_ids(f'label:"{name}" {school_window}', max_results=30)
            except Exception:
                log.exception("%s: failed searching School label %s for the School section", address, name)
                continue
            for mid in ids:
                if mid not in seen_ids:
                    seen_ids.add(mid)
                    candidate_ids.append(mid)
        candidate_ids = candidate_ids[:20]  # bound Gmail fetches + Gemini prompt size

        school_candidates = []
        school_lookup: dict[str, dict] = {}
        for mid in candidate_ids:
            # Already shown in "Needs a reply" - skip it here too, same
            # one-appearance-across-all-three-sections rule as Good to know
            # above. Only ever true for a candidate that was also part of
            # this run's inbox/folder review (a School-only fetch, below,
            # is never classified/checked for needs-reply in the first
            # place, so it can't be in this set).
            if f"inbox_{mid}" in needs_reply_refs or f"folder_{mid}" in needs_reply_refs:
                continue
            # Reuse data already fetched above (inbox/folder sweep) where
            # possible, to avoid a redundant Gmail fetch for the same email.
            reused = all_reviewed.get(f"inbox_{mid}") or all_reviewed.get(f"folder_{mid}")
            if reused:
                subject, sender, body, gmail_link = reused["subject"], reused["sender"], reused["body"], reused.get("gmail_link")
            else:
                try:
                    msg = gmail.get_message(mid)
                except Exception:
                    log.exception("%s: failed to fetch School candidate %s, skipping", address, mid)
                    continue
                subject, sender, body, gmail_link = msg.subject, msg.sender, msg.body_text, msg.permalink()
            ref = f"school_{mid}"
            # `body` is kept (not just a snippet) so a ranking failure below
            # can still build a clean, junk-free fallback excerpt from it
            # rather than falling back to Gmail's own raw snippet, which for
            # a forwarded email is often just its quoted "---------- Forwarded
            # message ---------" header block verbatim (see _clean_excerpt).
            school_lookup[ref] = {"subject": subject, "sender": sender, "gmail_link": gmail_link, "body": body}
            school_candidates.append({"ref": ref, "subject": subject, "sender": sender, "body": body})

        if school_candidates:
            try:
                ranked_school = ai.rank_and_summarize(school_candidates, top_n=3, tz=tz)
                for r in ranked_school:
                    base = school_lookup.get(r["ref"])
                    if not base:
                        continue
                    item = {**r, "subject": base["subject"], "sender": base["sender"], "gmail_link": base.get("gmail_link")}
                    item["calendar_link"] = _build_calendar_link(item, ics_store, bootstrap.public_base_url, address, recipient)
                    school_items.append(item)
            except Exception:
                log.exception("%s: School ranking failed, falling back to the most recent School emails", address)
                for c in school_candidates[:3]:
                    base = school_lookup[c["ref"]]
                    school_items.append({
                        "subject": base["subject"], "sender": base["sender"], "gmail_link": base.get("gmail_link"),
                        "summary": _clean_excerpt(base["body"]) or "Open in Gmail to see the full message.",
                        "is_event": False,
                    })

    # ---- 4. Build + send digest -----------------------------------------------------------
    now = datetime.now(ZoneInfo(tz))
    digest = DigestData(
        account_address=address,
        reviewed_count=len(all_reviewed),
        sorted_count=len(sorted_items),
        left_in_inbox_count=len(unmatched_items),
        needs_reply_count=len(needs_reply_items),
        needs_reply_items=needs_reply_items,
        top_important=top_picks,
        school_items=school_items,
        sorted_items=sorted_items,
        unmatched_items=unmatched_items,
        timezone=tz,
        date_label=now.strftime("%-d %B %Y"),
        frequency=frequency,
    )
    html_body = build_digest_html(digest)
    today = now.strftime("%Y-%m-%d")
    subject = DIGEST_SUBJECT_PREFIX_TEMPLATE.format(frequency=frequency, address=address) + today
    gmail.send_html_email(recipient, subject, html_body, address)
    log.info("%s: digest sent to %s", address, recipient)

    return {
        "reviewed": len(all_reviewed),
        "sorted": len(sorted_items),
        "left_in_inbox": len(unmatched_items),
        "needs_reply": len(needs_reply_items),
        "ok": True,
    }


def run_all(bootstrap: BootstrapConfig, settings_store: SettingsStore) -> None:
    """Runs every connected account once, recording results as it goes.
    A failure on one account is logged and recorded, never allowed to stop
    the others or crash the caller (the scheduler thread and the "run all"
    dashboard action both depend on that).

    A fresh AiClient is built per account (rather than one shared across
    all of them) so an account with its own Gemini key (see
    resolve_gemini_api_key) actually uses it, and so each account's
    pacing/circuit-breaker state (ai_client.py) reflects only its own
    calls rather than being skewed by another account's run moments
    earlier - the same isolation the scheduled/manual per-account runs
    already had."""
    settings = settings_store.get_settings()
    for account in settings_store.list_accounts():
        try:
            ai = AiClient(resolve_gemini_api_key(account, bootstrap), bootstrap.gemini_model)
            summary = run_account(account, bootstrap, settings, ai)
        except Exception:
            log.exception("Run failed for %s", account.get("address"))
            summary = {"ok": False, "error": "Run failed - see container logs."}
        settings_store.record_run_result(account["index"], summary)
