"""Thin Gmail REST API v1 wrapper.

Deliberately avoids the heavy google-api-python-client + its discovery
document fetch on every start; we call the REST endpoints directly with
`requests`, using google-auth purely for OAuth token storage/refresh
(no network client of its own is needed for that - it just POSTs to
Google's token endpoint).
"""
from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass, field
from email.mime.text import MIMEText

import requests
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials

log = logging.getLogger(__name__)

API_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"

# gmail.modify: list/search/read messages, apply labels, archive, mark read.
# gmail.compose: create drafts AND send mail (covers both the reply drafts
# and sending the digest email itself). Together these are narrower than
# the full "https://mail.google.com/" scope - no permanent delete access.
DEFAULT_SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.compose",
]

# System labels that are never valid "sort into this" targets - they're
# either not real folders (CATEGORY_*) or are the very state we're changing
# (INBOX/UNREAD) or not user-manageable via this flow (SPAM/TRASH).
NON_SORTABLE_LABELS = {
    "INBOX", "UNREAD", "SPAM", "TRASH", "DRAFT", "SENT", "CHAT",
    "CATEGORY_PERSONAL", "CATEGORY_SOCIAL", "CATEGORY_PROMOTIONS",
    "CATEGORY_UPDATES", "CATEGORY_FORUMS", "STARRED", "IMPORTANT",
}


@dataclass
class EmailMessage:
    id: str
    thread_id: str
    subject: str
    sender: str
    snippet: str
    body_text: str
    label_ids: list[str] = field(default_factory=list)
    # Epoch milliseconds Gmail actually received the message (its API
    # `internalDate` field, always present regardless of the `format`
    # requested) - used by pipeline.py's "hold unread mail" feature to work
    # out how many days old an unread inbox message is. Defaults to 0
    # (treated as "unknown age" by callers) so this stays optional for any
    # test/fake message that doesn't care about it.
    internal_date_ms: int = 0

    def permalink(self, account_index: int = 0) -> str:
        # Gmail's web UI accepts the API's message id directly as the
        # fragment identifier. `u/<index>` picks which signed-in Google
        # account slot to open it in - 0 is a reasonable default, but if
        # this isn't your first-signed-in account in the browser you may
        # need to bump the number (see README).
        return f"https://mail.google.com/mail/u/{account_index}/#all/{self.id}"


class GmailClient:
    def __init__(self, token_file: str):
        self.token_file = token_file
        self._creds: Credentials | None = None

    # ---- auth -----------------------------------------------------------
    def _load_credentials(self) -> Credentials:
        if self._creds and self._creds.valid:
            return self._creds

        with open(self.token_file, "r", encoding="utf-8") as fh:
            token_data = json.load(fh)

        creds = Credentials(
            token=token_data.get("token"),
            refresh_token=token_data["refresh_token"],
            token_uri="https://oauth2.googleapis.com/token",
            client_id=token_data["client_id"],
            client_secret=token_data["client_secret"],
            scopes=token_data.get("scopes", DEFAULT_SCOPES),
        )
        creds.refresh(GoogleAuthRequest())

        # Gmail refresh tokens don't expire from use, but persist the fresh
        # access token + scopes back so we don't hammer the token endpoint
        # on every single API call within a run.
        token_data["token"] = creds.token
        with open(self.token_file, "w", encoding="utf-8") as fh:
            json.dump(token_data, fh, indent=2)

        self._creds = creds
        return creds

    def _headers(self) -> dict:
        creds = self._load_credentials()
        return {"Authorization": f"Bearer {creds.token}"}

    def _get(self, path: str, params: dict | None = None) -> dict:
        resp = requests.get(f"{API_BASE}{path}", headers=self._headers(), params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, payload: dict) -> dict:
        resp = requests.post(f"{API_BASE}{path}", headers=self._headers(), json=payload, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def get_profile_email(self) -> str:
        """Returns the real Gmail address for whatever token this client was
        built with - used right after the OAuth callback to label a newly
        connected account, so the user never has to type their own address."""
        data = self._get("/profile")
        return data["emailAddress"]

    # ---- labels -----------------------------------------------------------
    def get_or_create_label(self, name: str) -> str:
        """Returns the id of the label `name` (matched case-insensitively,
        after trimming whitespace, against every label this account already
        has - Joe hit this for real: his existing label was "INBOX/Weekly
        Digest", a nested label whose full name/path IS "INBOX/Weekly
        Digest", not just "Weekly Digest" - matching on that exact full
        string, case-insensitively, is what finds an existing nested label
        like that instead of creating a near-duplicate top-level one).
        Creates it (visible, shown in the label list) only if truly nothing
        matches. Used for the "move old digests into a folder" setting so
        the configured label always exists rather than requiring the user
        to create it by hand first."""
        data = self._get("/labels")
        wanted = name.strip().lower()
        for lbl in data.get("labels", []):
            if lbl["name"].strip().lower() == wanted:
                return lbl["id"]
        created = self._post("/labels", {
            "name": name,
            "labelListVisibility": "labelShow",
            "messageListVisibility": "show",
        })
        return created["id"]

    def list_user_labels(self, ignore: set[str] | None = None) -> dict[str, str]:
        """Returns {label_name: label_id} for labels the AI may sort into -
        i.e. every label already in the account, minus system labels and
        anything in `ignore`."""
        ignore = ignore or set()
        data = self._get("/labels")
        result = {}
        for lbl in data.get("labels", []):
            name, lid = lbl["name"], lbl["id"]
            if lid in NON_SORTABLE_LABELS or name in NON_SORTABLE_LABELS:
                continue
            if name in ignore:
                continue
            result[name] = lid
        return result

    # ---- search / fetch -----------------------------------------------------------
    def search_message_ids(self, query: str, max_results: int = 50) -> list[str]:
        ids: list[str] = []
        page_token = None
        while True:
            params = {"q": query, "maxResults": min(max_results - len(ids), 100)}
            if page_token:
                params["pageToken"] = page_token
            data = self._get("/messages", params=params)
            ids.extend(m["id"] for m in data.get("messages", []))
            page_token = data.get("nextPageToken")
            if not page_token or len(ids) >= max_results:
                break
        return ids

    def get_message(self, message_id: str) -> EmailMessage:
        data = self._get(f"/messages/{message_id}", params={"format": "full"})
        headers = {h["name"].lower(): h["value"] for h in data["payload"].get("headers", [])}
        body = self._extract_body(data["payload"])
        try:
            internal_date_ms = int(data.get("internalDate", 0) or 0)
        except (TypeError, ValueError):
            internal_date_ms = 0
        return EmailMessage(
            id=data["id"],
            thread_id=data["threadId"],
            subject=headers.get("subject", "(no subject)"),
            sender=headers.get("from", "unknown sender"),
            snippet=data.get("snippet", ""),
            body_text=body,
            label_ids=data.get("labelIds", []),
            internal_date_ms=internal_date_ms,
        )

    def _extract_body(self, payload: dict, _depth: int = 0) -> str:
        """Best-effort plain-text extraction, preferring text/plain, falling
        back to a stripped text/html, walking multipart trees."""
        if _depth > 8:
            return ""
        mime_type = payload.get("mimeType", "")
        body_data = payload.get("body", {}).get("data")

        if mime_type == "text/plain" and body_data:
            return self._b64_decode(body_data)

        if mime_type == "text/html" and body_data and not payload.get("parts"):
            html = self._b64_decode(body_data)
            return _strip_html(html)

        parts = payload.get("parts", [])
        # Prefer a text/plain part if one exists anywhere in the tree.
        for part in parts:
            if part.get("mimeType") == "text/plain" and part.get("body", {}).get("data"):
                return self._b64_decode(part["body"]["data"])
        for part in parts:
            text = self._extract_body(part, _depth + 1)
            if text:
                return text
        return ""

    @staticmethod
    def _b64_decode(data: str) -> str:
        padded = data + "=" * (-len(data) % 4)
        try:
            return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - best-effort decoding
            return ""

    # ---- mutations -----------------------------------------------------------
    def apply_label_and_archive(self, message_id: str, label_id: str) -> None:
        self._post(f"/messages/{message_id}/modify", {"addLabelIds": [label_id], "removeLabelIds": ["INBOX"]})

    def mark_read(self, message_id: str) -> None:
        self._post(f"/messages/{message_id}/modify", {"removeLabelIds": ["UNREAD"]})

    def mark_unread(self, message_id: str) -> None:
        """Re-adds UNREAD - used to restore a message's unread status after
        it's been fully processed (classified, considered for the digest,
        possibly drafted a reply for) but held back from labelling/archiving
        by the per-account 'hold unread emails' grace period, so the user
        still sees it as unread in their inbox until they actually open it."""
        self._post(f"/messages/{message_id}/modify", {"addLabelIds": ["UNREAD"]})

    # ---- drafts -----------------------------------------------------------
    def has_existing_draft_for_thread(self, thread_id: str) -> str | None:
        """Returns the *underlying message id* of the existing draft on this
        thread (matching what create_draft_reply returns, so both feed
        draft_permalink() the same kind of id), or None if there isn't one.
        Checked before every create_draft call - a prior version of the
        equivalent Cowork-based digest created duplicate drafts on the same
        thread before this check was added, so this guard is load-bearing."""
        data = self._get("/drafts", params={"maxResults": 100})
        for d in data.get("drafts", []):
            msg = d.get("message", {})
            if msg.get("threadId") == thread_id:
                return msg.get("id")
        return None

    def create_draft_reply(self, original: EmailMessage, reply_body: str, account_address: str) -> str:
        """Creates a draft reply on the same thread. Returns the draft's
        underlying message id (used to build a Gmail permalink)."""
        mime_msg = MIMEText(reply_body)
        mime_msg["to"] = original.sender
        mime_msg["from"] = account_address
        subject = original.subject
        if not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"
        mime_msg["subject"] = subject
        mime_msg["In-Reply-To"] = original.id
        raw = base64.urlsafe_b64encode(mime_msg.as_bytes()).decode("ascii")

        result = self._post("/drafts", {"message": {"raw": raw, "threadId": original.thread_id}})
        return result["message"]["id"]

    @staticmethod
    def draft_permalink(draft_message_id: str, account_index: int = 0) -> str:
        return f"https://mail.google.com/mail/u/{account_index}/#drafts/{draft_message_id}"

    # ---- sending the digest itself -----------------------------------------------------------
    def send_html_email(self, to_address: str, subject: str, html_body: str, from_address: str) -> None:
        mime_msg = MIMEText(html_body, "html")
        mime_msg["to"] = to_address
        mime_msg["from"] = from_address
        mime_msg["subject"] = subject
        raw = base64.urlsafe_b64encode(mime_msg.as_bytes()).decode("ascii")
        self._post("/messages/send", {"raw": raw})


def _strip_html(html: str) -> str:
    """Extremely small HTML->text fallback (no lxml/bs4 dependency, keeps the
    image lightweight) - good enough for feeding email bodies to the AI
    classifier, not meant to be pretty."""
    import re

    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()
