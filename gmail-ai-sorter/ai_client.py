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

import requests

log = logging.getLogger(__name__)

API_URL_TMPL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


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
        resp = requests.post(url, params={"key": self.api_key}, json=payload, timeout=60)
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
