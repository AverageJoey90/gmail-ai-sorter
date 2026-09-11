"""The actual sort + digest run for one Gmail account. Used by both the
daily background scheduler and the dashboard's "Run now" button, so there
is exactly one implementation of the logic described in the project spec.
"""
from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from ai_client import AiClient
from config import BootstrapConfig
from digest_builder import DigestData, build_digest_html
from gmail_client import GmailClient
from oauth_web import token_path_for
from settings_store import SettingsStore

log = logging.getLogger(__name__)


def run_account(account: dict, bootstrap: BootstrapConfig, settings: dict, ai: AiClient) -> dict:
    """Runs one full sort+digest cycle for `account` (a record from
    SettingsStore.list_accounts()). Returns a small summary dict suitable
    for SettingsStore.record_run_result and for display in the dashboard."""
    address = account["address"]
    index = account["index"]
    log.info("Starting run for %s", address)

    token_file = token_path_for(bootstrap, index)
    gmail = GmailClient(token_file)

    ignore_labels = set(settings.get("ignore_labels", []))
    threshold = float(settings.get("classify_confidence_threshold", 0.7))
    tz = settings.get("timezone", "Europe/London")
    # Anything classified into (or already filed under) one of these labels
    # always makes it into the digest's "Good to know" section, regardless
    # of the AI's top-5 importance ranking - e.g. school emails, so they're
    # never at the mercy of the ranking call judging them less important
    # than something else that happened that day.
    always_important_labels = {s.strip().lower() for s in settings.get("always_important_labels", []) if s.strip()}

    label_map = gmail.list_user_labels(ignore=ignore_labels)
    label_names = list(label_map.keys())
    log.info("%s: %d existing labels available to sort into", address, len(label_names))

    needs_reply_items: list[dict] = []
    sorted_items: list[dict] = []
    unmatched_items: list[dict] = []
    priority_items: list[dict] = []  # always-important-label matches, force-merged into "Good to know" below
    all_reviewed: dict[str, dict] = {}  # ref -> {subject, sender, body}

    def maybe_flag_needs_reply(msg, ai_result: dict) -> None:
        if not ai_result.get("needs_reply"):
            return
        draft_link = None
        existing = gmail.has_existing_draft_for_thread(msg.thread_id)
        if existing:
            draft_link = GmailClient.draft_permalink(existing)
        else:
            try:
                reply_text = ai.draft_reply(msg.subject, msg.sender, msg.body_text)
                draft_msg_id = gmail.create_draft_reply(msg, reply_text, address)
                draft_link = GmailClient.draft_permalink(draft_msg_id)
            except Exception:
                log.exception("%s: failed to create draft for message %s", address, msg.id)
        needs_reply_items.append({
            "subject": msg.subject,
            "sender": msg.sender,
            "summary": ai_result.get("reasoning") or msg.snippet,
            "draft_link": draft_link,
        })

    # ---- 1. Inbox sort -----------------------------------------------------------
    inbox_ids = gmail.search_message_ids("in:inbox", max_results=200)
    log.info("%s: %d messages currently in inbox", address, len(inbox_ids))
    for msg_id in inbox_ids:
        try:
            msg = gmail.get_message(msg_id)
        except Exception:
            log.exception("%s: failed to fetch inbox message %s, skipping", address, msg_id)
            continue

        all_reviewed[f"inbox_{msg.id}"] = {
            "ref": f"inbox_{msg.id}", "subject": msg.subject, "sender": msg.sender,
            "body": msg.body_text, "gmail_link": msg.permalink(),
        }

        try:
            result = ai.classify_email(msg.subject, msg.sender, msg.body_text, label_names)
        except Exception:
            log.exception("%s: classification failed for %s, leaving in inbox", address, msg_id)
            unmatched_items.append({
                "subject": msg.subject, "sender": msg.sender,
                "reason": "Classification failed - see container logs.", "gmail_link": msg.permalink(),
            })
            continue

        if always_important_labels and result.get("label") and result["label"].strip().lower() in always_important_labels:
            priority_items.append({
                "subject": msg.subject,
                "sender": msg.sender,
                "summary": result.get("reasoning") or msg.snippet,
                "gmail_link": msg.permalink(),
                "is_event": False,
            })

        if result["label"] and result["confidence"] >= threshold:
            try:
                gmail.apply_label_and_archive(msg.id, label_map[result["label"]])
                sorted_items.append({
                    "subject": msg.subject, "sender": msg.sender,
                    "label": result["label"], "gmail_link": msg.permalink(),
                })
            except Exception:
                log.exception("%s: failed to apply label to %s", address, msg_id)
                unmatched_items.append({
                    "subject": msg.subject, "sender": msg.sender,
                    "reason": "Label apply failed - see container logs.", "gmail_link": msg.permalink(),
                })
        else:
            reason = result.get("reasoning") or "No existing label was a confident match."
            unmatched_items.append({
                "subject": msg.subject, "sender": msg.sender,
                "reason": reason, "gmail_link": msg.permalink(),
            })

        maybe_flag_needs_reply(msg, result)

    # ---- 2. Sweep other labelled folders for unread mail -----------------------------------------------------------
    folder_unread_ids: set[str] = set()
    priority_folder_ids: set[str] = set()  # unread ids that live under an always-important label (e.g. "School")
    for name in label_names:
        try:
            ids = gmail.search_message_ids(f'label:"{name}" is:unread', max_results=100)
        except Exception:
            log.exception("%s: failed searching label %s, skipping", address, name)
            continue
        folder_unread_ids.update(ids)
        if always_important_labels and name.strip().lower() in always_important_labels:
            priority_folder_ids.update(ids)

    log.info("%s: %d unread messages found across %d labelled folders", address, len(folder_unread_ids), len(label_names))
    for msg_id in folder_unread_ids:
        try:
            msg = gmail.get_message(msg_id)
            gmail.mark_read(msg_id)
        except Exception:
            log.exception("%s: failed processing folder message %s, skipping", address, msg_id)
            continue

        all_reviewed[f"folder_{msg.id}"] = {
            "ref": f"folder_{msg.id}", "subject": msg.subject, "sender": msg.sender,
            "body": msg.body_text, "gmail_link": msg.permalink(),
        }

        if msg_id in priority_folder_ids:
            priority_items.append({
                "subject": msg.subject,
                "sender": msg.sender,
                "summary": msg.snippet,
                "gmail_link": msg.permalink(),
                "is_event": False,
            })

        try:
            result = ai.classify_email(msg.subject, msg.sender, msg.body_text, label_names)
            maybe_flag_needs_reply(msg, result)
        except Exception:
            log.exception("%s: needs-reply check failed for folder message %s", address, msg_id)

    # ---- 3. Top 5 important + event detection -----------------------------------------------------------
    top_picks = []
    if all_reviewed:
        try:
            ranked = ai.rank_and_summarize(list(all_reviewed.values()), top_n=5, tz=tz)
            for r in ranked:
                base = all_reviewed.get(r["ref"])
                if not base:
                    continue
                top_picks.append({**r, "subject": base["subject"], "sender": base["sender"], "gmail_link": base.get("gmail_link")})
        except Exception:
            log.exception("%s: importance ranking failed, omitting 'Good to know' section", address)

    # Force-merge always-important-label matches (e.g. School) to the front
    # of "Good to know", ahead of the AI's own picks - but don't duplicate
    # one that the ranking already picked up with its own (better) summary.
    if priority_items:
        seen = {(p.get("subject"), p.get("sender")) for p in top_picks}
        priority_only = []
        for p in priority_items:
            key = (p.get("subject"), p.get("sender"))
            if key in seen:
                continue
            seen.add(key)
            priority_only.append(p)
        top_picks = priority_only + top_picks

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
        sorted_items=sorted_items,
        unmatched_items=unmatched_items,
        timezone=tz,
        date_label=now.strftime("%-d %B %Y"),
    )
    html_body = build_digest_html(digest)
    today = now.strftime("%Y-%m-%d")
    subject = f"Gmail daily digest - {address} - {today}"
    recipient = account.get("digest_recipient") or address
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
    dashboard action both depend on that)."""
    ai = AiClient(bootstrap.gemini_api_key, bootstrap.gemini_model)
    settings = settings_store.get_settings()
    for account in settings_store.list_accounts():
        try:
            summary = run_account(account, bootstrap, settings, ai)
        except Exception:
            log.exception("Run failed for %s", account.get("address"))
            summary = {"ok": False, "error": "Run failed - see container logs."}
        settings_store.record_run_result(account["index"], summary)
