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

# ---- proactive pacing + a grounded "we're clearly rate-limited" circuit breaker ----
# None of this changes what any single call is allowed to do or how many
# times it's retried - MAX_RETRIES above is untouched, so an individual
# email's classification outcome is never affected. It only changes how
# eagerly calls are fired and, in one narrow case backed by Google's own
# explicit signal (not a guess), skips a call that has already proven it
# cannot succeed - so a busy run finishes faster without ever producing a
# different result than the unthrottled version would eventually reach.
#
# Google's free tier for gemini-2.5-flash is only ~10 requests/minute (see
# the comment above), so a baseline gap of 6.5s between calls keeps a
# single AiClient under that on its own, without needing to guess how many
# other calls might be sharing the same key concurrently.
BASE_PACE_SECONDS = 6.5
MAX_PACE_SECONDS = 30.0
# Any call that needed at least one retry grows the gap before the next
# call (we're clearly bumping the limit); a clean call with zero retries
# gradually relaxes it back down. This is what actually stops a burst of
# calls (e.g. sweeping 25 folder messages) from ever turning into the kind
# of 429 storm seen in the logs, rather than just reacting to it faster.
PACE_GROWTH_FACTOR = 1.6
PACE_DECAY_FACTOR = 0.85

# Google's RetryInfo can say "wait" anywhere from a few seconds (an
# ordinary per-minute limit) to many minutes (the *daily* quota, which
# nothing inside one run can fix). A single occurrence of a long suggested
# delay is NOT enough to skip retrying that call early - it might be
# conservative, or the limit might ease before this call's own retries run
# out - so every call still gets its full, unmodified MAX_RETRIES/
# MAX_DELAY_SECONDS treatment and its outcome is never guessed at. This
# threshold only marks the signal for bookkeeping (see
# LONG_OUTAGE_TRIP_THRESHOLD below).
GIVE_UP_DELAY_SECONDS = 90.0
# Only once several DIFFERENT calls in a row have each been retried in
# full and still failed, each with that long-delay signal, do we treat it
# as real, sustained evidence and start skipping the network call entirely
# on FURTHER calls for a while - Google has, by then, told us repeatedly
# and consistently that nothing will succeed right now. One real "probe"
# call is still let through every PROBE_INTERVAL_SECONDS so a recovered
# quota is noticed automatically; a probe is always a genuine attempt,
# never a synthetic failure, so this can only skip calls that several real,
# fully-retried attempts already proved would fail - it never shortens or
# guesses at any individual call's own retry budget.
LONG_OUTAGE_TRIP_THRESHOLD = 3
PROBE_INTERVAL_SECONDS = 90.0


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
        # Pacing/circuit-breaker state - see the constants above. Lives on
        # the instance (not module-level) so each AiClient - one per
        # account per scheduled run, or one shared across accounts in a
        # manual "run all" - paces only against calls it made itself.
        self._pace_seconds = BASE_PACE_SECONDS
        self._last_call_at = 0.0
        self._long_outage_streak = 0
        self._last_probe_at = 0.0

    def _pace(self) -> None:
        """Sleeps just long enough since this instance's last call to
        respect the current pacing gap, then records the new call time."""
        now = time.monotonic()
        wait = self._pace_seconds - (now - self._last_call_at)
        if wait > 0:
            time.sleep(wait)
        self._last_call_at = time.monotonic()

    def _circuit_is_open(self) -> bool:
        """True if the last several calls all hit Google's "wait minutes"
        signal, so this call should be skipped without even trying -
        unless it's time for a periodic probe to check whether that's
        resolved. See LONG_OUTAGE_TRIP_THRESHOLD/PROBE_INTERVAL_SECONDS."""
        if self._long_outage_streak < LONG_OUTAGE_TRIP_THRESHOLD:
            return False
        now = time.monotonic()
        if now - self._last_probe_at >= PROBE_INTERVAL_SECONDS:
            self._last_probe_at = now
            return False  # let exactly this one through as a probe
        return True

    def _record_success(self) -> None:
        self._pace_seconds = max(BASE_PACE_SECONDS, self._pace_seconds * PACE_DECAY_FACTOR)
        self._long_outage_streak = 0

    def _record_retry_needed(self) -> None:
        self._pace_seconds = min(MAX_PACE_SECONDS, self._pace_seconds * PACE_GROWTH_FACTOR)

    def _record_long_outage(self) -> None:
        self._record_retry_needed()
        self._long_outage_streak += 1
        if self._long_outage_streak == LONG_OUTAGE_TRIP_THRESHOLD:
            # Just tripped - start the probe clock from right now, not from
            # this instance's arbitrary "never probed" starting point
            # (_last_probe_at's default of 0.0), so the very first check
            # right after tripping doesn't look like a probe is already
            # overdue and let a call straight through untripped.
            self._last_probe_at = time.monotonic()

    def _generate(self, prompt: str, response_schema: dict | None = None, temperature: float = 0.2) -> dict | str:
        if self._circuit_is_open():
            raise RuntimeError(
                "Gemini API has told us repeatedly to wait minutes (quota exhausted) - "
                "skipping this call so it fails immediately instead of after another doomed "
                "multi-minute retry; it's re-checked periodically and will resume automatically."
            )

        payload: dict = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": temperature},
        }
        if response_schema is not None:
            payload["generationConfig"]["responseMimeType"] = "application/json"
            payload["generationConfig"]["responseSchema"] = response_schema

        url = API_URL_TMPL.format(model=self.model)

        self._pace()
        attempt = 0
        # Whether Google told us, on any attempt of THIS call, that it
        # wants a genuinely long wait (see GIVE_UP_DELAY_SECONDS) - noted
        # for bookkeeping only. This never shortens or skips this call's
        # own retry loop below, which is untouched from before this round
        # of changes (still up to MAX_RETRIES, still capped at
        # MAX_DELAY_SECONDS) - so a single email's outcome is never guessed
        # at. It only feeds _long_outage_streak, which can make a *later*,
        # different call skip early - and only after several of these in a
        # row have already proven, for real, that retrying doesn't help.
        had_long_delay_signal = False
        while True:
            resp = requests.post(url, params={"key": self.api_key}, json=payload, timeout=60)
            if resp.status_code in RETRYABLE_STATUS_CODES:
                raw_delay = _retry_delay_from_response(resp)
                if raw_delay is not None and raw_delay > GIVE_UP_DELAY_SECONDS:
                    had_long_delay_signal = True
                if attempt < MAX_RETRIES:
                    delay = raw_delay if raw_delay is not None else BASE_DELAY_SECONDS * (2 ** attempt)
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

        if resp.status_code in RETRYABLE_STATUS_CODES:
            # Exhausted every retry - noting, for future calls, whether
            # this exhaustion came with a "this is a long outage" signal.
            if had_long_delay_signal:
                self._record_long_outage()
            else:
                self._record_retry_needed()

        # Unconditional, exactly as before this round of changes: raises
        # the same HTTPError callers already catch for a retryable status
        # that exhausted its retries AND for a non-retryable status (e.g.
        # 403) that was never retried at all.
        resp.raise_for_status()

        self._long_outage_streak = 0  # any success is proof we're not (or no longer) in a long outage
        if attempt > 0:
            self._record_retry_needed()
        else:
            self._record_success()

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
        """Returns {"label": str|None, "confidence": float, "needs_reply": bool,
        "summary": str}. `summary` is a one-line, plain description of what
        the email is actually about (used in the digest's "Needs a reply"
        section) - kept separate from `reasoning`, which explains the label
        choice specifically and can read oddly out of that context."""
        schema = {
            "type": "object",
            "properties": {
                "best_label": {"type": "string", "enum": label_names + ["__NONE__"]},
                "confidence": {"type": "number"},
                "needs_reply": {"type": "boolean"},
                "reasoning": {"type": "string"},
                "summary": {"type": "string"},
            },
            "required": ["best_label", "confidence", "needs_reply", "summary"],
        }
        prompt = f"""You are sorting one email into an existing Gmail label, from this
fixed list of labels already used in this mailbox (never invent a new one):
{json.dumps(label_names)}

If none of them genuinely fit, respond with best_label "__NONE__".

Also decide whether this email needs a personal reply from the recipient
(ignore no-reply/notification/marketing mail, receipts, and anything that
is purely informational).

Also write `summary`: a single plain sentence, IN YOUR OWN WORDS, describing
what this email is actually about (e.g. "Colleague asking to confirm
Thursday's 3pm meeting") - independent of the labelling decision, since
this is shown to the recipient directly. Never quote or copy the raw
"---------- Forwarded message ---------" header block, "On ... wrote:"
reply-quote lines, timestamps, or raw email addresses from the message -
describe the content itself, not its envelope.

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
            "summary": result.get("summary", ""),
        }

    # ---- classification, several at once -----------------------------------------------------------
    def classify_emails_batch(self, emails: list[dict], label_names: list[str]) -> dict[str, dict]:
        """Same job as classify_email, for several emails in one Gemini call
        instead of one call each - the fix for the 429 storms seen sweeping
        a folder with many messages in it (one call per message very
        quickly outran the free tier's ~10 requests/minute). `emails` is a
        list of {"ref": str, "subject": str, "sender": str, "body": str}.
        Returns {ref: result}, where each result has EXACTLY the same shape
        classify_email returns for that one email - the prompt explicitly
        tells the model to judge every email independently of the others in
        the batch, so batching is purely a transport-level optimisation and
        shouldn't change any individual email's classification. A ref
        missing from the model's response (or the whole call failing) is
        left out of the returned dict - callers treat a missing ref exactly
        like a classify_email failure for that message."""
        if not emails:
            return {}
        schema = {
            "type": "object",
            "properties": {
                "results": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "ref": {"type": "string"},
                            "best_label": {"type": "string", "enum": label_names + ["__NONE__"]},
                            "confidence": {"type": "number"},
                            "needs_reply": {"type": "boolean"},
                            "reasoning": {"type": "string"},
                            "summary": {"type": "string"},
                        },
                        "required": ["ref", "best_label", "confidence", "needs_reply", "summary"],
                    },
                }
            },
            "required": ["results"],
        }
        catalogue = "\n\n".join(
            f"[{e['ref']}] Subject: {e['subject']}\nFrom: {e['sender']}\nBody (truncated): {e['body'][:4000]}"
            for e in emails
        )
        prompt = f"""You are sorting SEVERAL emails - each tagged with a [ref] id - into an
existing Gmail label, from this fixed list of labels already used in this
mailbox (never invent a new one):
{json.dumps(label_names)}

Judge every email COMPLETELY INDEPENDENTLY of the others below - they are
unrelated and just happen to be batched into one request for efficiency.
Nothing about one email should influence your decision on another. For
each one, if none of the labels genuinely fit, respond with best_label
"__NONE__".

For each email also decide whether it needs a personal reply from the
recipient (ignore no-reply/notification/marketing mail, receipts, and
anything that is purely informational), and write `summary`: a single
plain sentence, IN YOUR OWN WORDS, describing what that email is actually
about (e.g. "Colleague asking to confirm Thursday's 3pm meeting") -
independent of the labelling decision, since this may be shown to the
recipient directly. Never quote or copy the raw "---------- Forwarded
message ---------" header block, "On ... wrote:" reply-quote lines,
timestamps, or raw email addresses from the message - describe the
content itself, not its envelope.

Return exactly one entry in `results` for every [ref] below, each carrying
its own `ref` value unchanged so it can be matched back up.

{catalogue}
"""
        result = self._generate(prompt, response_schema=schema)
        out: dict[str, dict] = {}
        for r in result.get("results", []):
            ref = r.get("ref")
            if not ref:
                continue
            label = r.get("best_label")
            if label == "__NONE__":
                label = None
            out[ref] = {
                "label": label,
                "confidence": float(r.get("confidence", 0)),
                "needs_reply": bool(r.get("needs_reply", False)),
                "reasoning": r.get("reasoning", ""),
                "summary": r.get("summary", ""),
            }
        return out

    # ---- draft replies -----------------------------------------------------------
    def draft_reply(self, subject: str, sender: str, body: str, user_name: str = "Joe") -> dict:
        """Returns {"reply_body": str, "reply_gist": str}. `reply_gist` is a
        single plain sentence paraphrasing what the drafted reply actually
        says (e.g. "Confirms Thursday 3pm works"), shown in the digest so
        the gist is visible without opening the draft in Gmail."""
        schema = {
            "type": "object",
            "properties": {
                "reply_body": {"type": "string"},
                "reply_gist": {"type": "string"},
            },
            "required": ["reply_body", "reply_gist"],
        }
        prompt = f"""Write a short, polite, ready-to-send draft email reply from {user_name} to
the email below. Match the sender's tone/formality. Keep it concise
(a few sentences), don't invent facts or commitments the original email
doesn't support, and leave a placeholder like [confirm details] if a real
answer requires information you don't have.

Return JSON with two fields: `reply_body` (ONLY the reply body text - no
subject line, no explanation of what you did) and `reply_gist` (a single
plain sentence paraphrasing what that reply actually says, e.g. "Confirms
Thursday 3pm works" or "Asks for a couple more days to decide").

Subject: {subject}
From: {sender}
Body: {body[:4000]}
"""
        result = self._generate(prompt, response_schema=schema)
        return {
            "reply_body": result.get("reply_body", "").strip(),
            "reply_gist": result.get("reply_gist", "").strip(),
        }

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
for each write a 1-2 sentence summary, IN YOUR OWN WORDS, describing what
it's actually about. Never quote or copy a "---------- Forwarded message
---------" header block, "On ... wrote:" reply-quote lines, timestamps, or
raw email addresses into the summary - describe the content itself, not
its envelope, and keep it short and plain.

Only set is_event true for a genuine scheduled occurrence the reader would
actually attend, turn up to, or participate in at a specific date/time -
a meeting, appointment, reservation, class, performance, party, or similar.
It must invite actual attendance, not just mention a date. A payment or
task DEADLINE, a delivery/dispatch date, a subscription renewal, or a
vague mention of "next week" with nothing to attend is NOT an event, even
though it has a date attached - leave is_event false for those (still
summarize them normally). When it genuinely is an event, fill in
event_title, event_start_iso8601 (and event_end_iso8601 if
stated/inferable - default to 1 hour after start if only a start time is
given), and event_location if mentioned. Use the {tz} timezone if no
timezone is stated in the email. Otherwise leave is_event false and omit
the event_* fields.

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
