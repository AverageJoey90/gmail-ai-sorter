"""Thin Todoist API wrapper - just enough to create a task, for the nightly
"is the tunnel still live" alert (round 32, Joe's request) and its "Test
Todoist now" button on the dashboard.

Round 32.1: the older REST v2 endpoint this originally used
(api.todoist.com/rest/v2/tasks) returns a hard 410 Gone as of Todoist's
API v1.0 migration (announced for Q4 2025 - confirmed live against Joe's
real key, not just from the announcement) - Todoist merged their old
Sync and REST APIs into one unified API under /api/v1/. The simple
single-task-creation call used here still exists in the new API, just at
a new path (developer.todoist.com's own quickstart example), with the
same auth, field names (`content`/`due_string`/`project_id`), and
response shape (the created task's JSON, with an `id`) - only the base
URL changed.

Deliberately minimal, same philosophy as gmail_client.py: plain `requests`
calls against the REST endpoint rather than pulling in Todoist's own SDK for
one call.
"""
from __future__ import annotations

import logging

import requests

log = logging.getLogger(__name__)

API_URL = "https://api.todoist.com/api/v1/tasks"


class TodoistError(RuntimeError):
    """Raised with a message that's safe and useful to show directly on the
    dashboard (a flash banner) - not just "see container logs" - since this
    is almost always a wrong/revoked API key or a wrong project ID, both of
    which Joe can fix himself right there on the Settings card."""


class TodoistClient:
    def __init__(self, api_key: str):
        self.api_key = api_key

    def create_task(self, content: str, project_id: str = "", due_string: str = "today") -> dict:
        """Creates a task via Todoist's REST API. `project_id` left blank
        (the default) files it in whatever Todoist considers the account's
        default/Inbox project - Todoist's own behaviour when the field is
        omitted entirely, not something this wrapper decides. Raises
        TodoistError with a readable reason on any failure (bad/missing key,
        unknown project id, no network, etc.) - never a bare requests
        exception, since the caller may be putting this straight into a
        flash message."""
        if not self.api_key:
            raise TodoistError("No Todoist API key is set - add one on the Settings card first.")

        payload: dict = {"content": content}
        if due_string:
            payload["due_string"] = due_string
        if project_id:
            payload["project_id"] = project_id

        try:
            resp = requests.post(
                API_URL,
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload,
                timeout=15,
            )
        except requests.RequestException as exc:
            raise TodoistError(f"Couldn't reach Todoist: {exc}") from exc

        if resp.status_code == 401:
            raise TodoistError("Todoist rejected the API key (401 Unauthorized) - check it's correct and still active.")
        if resp.status_code == 403:
            raise TodoistError("Todoist refused the request (403 Forbidden) - check the API key's permissions.")
        if resp.status_code == 404:
            raise TodoistError("Todoist couldn't find that project (404 Not Found) - check the project ID, or leave it blank to use your Inbox.")
        if not resp.ok:
            detail = (resp.text or "").strip()[:200]
            raise TodoistError(f"Todoist returned {resp.status_code}{': ' + detail if detail else ''}.")

        try:
            return resp.json()
        except ValueError:
            return {}
