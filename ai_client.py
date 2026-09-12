"""Gemini API client used for classification, summarization, draft replies
and importance ranking.

Uses the plain REST endpoint (generativelanguage.googleapis.com) via
`requests` rather than the official google-genai SDK, specifically to avoid
pulling in grpcio (heavy, and historically fiddly to get working on ARM64) -
the REST API supports everything this app needs, including structured
JSON output via responseSchema.

Why Gemini: it currently has the most usable free tier of the major AI
APIs with no payment method required to start (Anthropic and OpenAI's free
allowances are trial credits that expire; Gemini's free tier is ongoing,
rate-limited rather than credit-limited). Trade-off worth knowing: Google's
terms say free-tier traffic (unlike paid-tier) may be used to improve their
products, which matters here since the input is your email content - see
the README for how to switch to a paid key (or Groq, or Anthropic) later
if that trade-off doesn't sit right with you.
"""
from __future__ import annotations

import json
import logging
import random
import time

import requests

log = logging.getLogger(__name__)

API_URL_TMPL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# Google's free tier for gemini-2.5-flash is only ~10 requests/minute, and
# with two Gmail accounts sharing one key it's easy to burst past that
# during a busy run - see 429 Too Many Requests in the container logs.
# Retried here rather than left to fail immediately: 429 (rate limit) and
# 503 (temporarily overloaded) are both transient per Google's own
# troubleshooting guidance, unlike a 400 (bad request) or 403 (bad key),
# which are retried exactly as often (never).
RETRYABLE_STATUS_CODES = {429, 503}
MAX_RETRIES = 4
BASE_DELAY_SECONDS = 1.0
# Cap any single wait (whether from our own backoff or a server-suggested
# delay) at 30s - if Google's error body says to wait minutes (e.g. the
# *daily* quota, not just the per-minute one, is exhausted), no amount of
# retrying within one run will help, so fail fast and let this email fall
# through to the existing "left in inbox, will retry next run" handling
# instead of stalling the whole account's run.
MAX_DELAY_SECONDS = 30.0


def _retry_delay_from_response(resp: requests.Response) -> float | None:
    """Google's 429/503 error bodies can carry a machine-readable
    google.rpc.RetryInfo with a retryDelay (e.g. "53s") telling you exactly
    how long to wait - honor that over guessing with plain backoff when
    it's present, since (as of writing) even Google's own official SDK
    doesn't actually parse/use it. Returns None if the body isn't JSON or
    doesn't carry one, so the caller falls back to exponential backoff."""
    try:
        details = resp.json().get("error", {}).get("details", [])
    except ValueError:
        return None
    for d in details:
        if str(d.get("@type", "")).endswith("RetryInfo"):
            raw = str(d.get("retryDelay", ""))
            if raw.endswith("s"):
                try:
                    return float(raw[:-1])
                except ValueError:
                    pass
    return None


class AiClient:
    def __init__(self, api_key: str, model: str = "gemini-2.5-flash"):
        if not api_key:
            raise SystemExit("GEMINI_API_KEY is not set - get a free key at https://aistudio.google.com/apikey")
        self.api_key = api_key
        self.model = model

    def _generate(self, prompt: str, response_schema: dict | None = None, temperature: float = 0.2) -> dict | str:
        payload: dict = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": temperature},
        }
        if response_schema is not None:
            payload["generationConfig"]["responseMimeType"] = "application/json"
            payload["generationConfig"]["responseSchema"] = response_schema

        url = API_URL_TMPL.format(model=self.model)

        attempt = 0
        while True:
            resp = requests.post(url, params={"key": self.api_key}, json=payload, timeout=60)
            if resp.status_code in RETRYABLE_STATUS_CODES and attempt < MAX_RETRIES:
                delay = _retry_delay_from_response(resp)
                if delay is None:
                    delay = BASE_DELAY_SECONDS * (2 ** attempt)
                delay = min(delay, MAX_DELAY_SECONDS)
                delay += random.uniform(0, delay * 0.25)  # jitter, avoids lock-step retries
                attempt += 1
                log.warning(
                    "Gemini API returned %d (attempt %d/%d) - retrying in %.1fs",
                    resp.status_code, attempt, MAX_RETRIES, delay,
                )
                time.sleep(delay)
                continue
            break

        resp.raise_for_status()
        data = resp.json()
        try:
            text = data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError) as exc:
            raise RuntimeError(f"Unexpected Gemini response shape: {data}") from exc

        if response_schema is not None:
            return json.loads(text)
        return text

    # ---- classification -----------------------------------------------------------
    def classify_email(self, subject: str, sender: str, body: str, label_names: list[str]) -> dict:
        """Returns {"label": str|None, "confidence": float, "needs_reply": bool}."""
        schema = {
            "type": "object",
            "properties": {
                "best_label": {"type": "string", "enum": label_names + ["__NONE__"]},
                "confidence": {"type": "number"},
                "needs_reply": {"type": "boolean"},
                "reasoning": {"type": "string"},
            },
            "required": ["best_label", "confidence", "needs_reply"],
        }
        prompt = f"""You are sorting one email into an existing Gmail label, from this
fixed list of labels already used in this mailbox (never invent a new one):
{json.dumps(label_names)}

If none of them genuinely fit, respond with best_label "__NONE__".

Also decide whether this email needs a personal reply from the recipient
(ignore no-reply/notification/marketing mail, receipts, and anything that
is purely informational).

Email:
Subject: {subject}
From: {sender}
Body (truncated): {body[:4000]}
"""
        result = self._generate(prompt, response_schema=schema)
        label = result.get("best_label")
        if label == "__NONE__":
            label = None
        return {
            "label": label,
            "confidence": float(result.get("confidence", 0)),
            "needs_reply": bool(result.get("needs_reply", False)),
            "reasoning": result.get("reasoning", ""),
        }

    # ---- draft replies -----------------------------------------------------------
    def draft_reply(self, subject: str, sender: str, body: str, user_name: str = "Joe") -> str:
        prompt = f"""Write a short, polite, ready-to-send draft email reply from {user_name} to
the email below. Match the sender's tone/formality. Keep it concise
(a few sentences), don't invent facts or commitments the original email
doesn't support, and leave a placeholder like [confirm details] if a real
answer requires information you don't have. Output ONLY the reply body
text, no subject line, no "Dear/Hi" salutation preamble explanation.

Subject: {subject}
From: {sender}
Body: {body[:4000]}
"""
        return self._generate(prompt).strip()

    # ---- importance ranking + summaries -----------------------------------------------------------
    def rank_and_summarize(self, emails: list[dict], top_n: int = 5, tz: str = "Europe/London") -> list[dict]:
        """`emails` is a list of {"ref": str, "subject": str, "sender": str,
        "body": str}. Returns up to top_n items, most important first:
        {"ref", "summary", "is_event", "event_title", "event_start",
        "event_end", "event_location"} (event_* fields blank when
        is_event is false). event_start/end are ISO 8601 if known."""
        if not emails:
            return []
        schema = {
            "type": "object",
            "properties": {
                "picks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "ref": {"type": "string"},
                            "summary": {"type": "string"},
                            "is_event": {"type": "boolean"},
                            "event_title": {"type": "string"},
                            "event_start_iso8601": {"type": "string"},
                            "event_end_iso8601": {"type": "string"},
                            "event_location": {"type": "string"},
                        },
                        "required": ["ref", "summary", "is_event"],
                    },
                }
            },
            "required": ["picks"],
        }
        catalogue = "\n\n".join(
            f"[{e['ref']}] Subject: {e['subject']}\nFrom: {e['sender']}\nBody: {e['body'][:2000]}"
            for e in emails
        )
        prompt = f"""Below are emails reviewed in one mailbox sweep, each tagged with a
[ref] id. Pick the {top_n} MOST IMPORTANT ones overall (skip routine
notifications/marketing/automated mail unless genuinely important) and
for each write a 1-2 sentence summary.

If an email describes a specific dated event, meeting, appointment,
reservation, or deadline, set is_event true and fill in event_title,
event_start_iso8601 (and event_end_iso8601 if stated/inferable - default
to 1 hour after start if only a start time is given), and
event_location if mentioned. Use the {tz} timezone if no timezone is
stated in the email. Otherwise leave is_event false and omit the event_*
fields.

Return picks ordered most important first, referencing each by its [ref] id.

{catalogue}
"""
        result = self._generate(prompt, response_schema=schema)
        picks = result.get("picks", [])[:top_n]
        # Normalise field names/defaults so callers don't need to guess which
        # optional keys are present.
        normalised = []
        for p in picks:
            normalised.append({
                "ref": p.get("ref", ""),
                "summary": p.get("summary", ""),
                "is_event": bool(p.get("is_event", False)),
                "event_title": p.get("event_title", ""),
                "event_start": p.get("event_start_iso8601", ""),
                "event_end": p.get("event_end_iso8601", ""),
                "event_location": p.get("event_location", ""),
            })
        return normalised
