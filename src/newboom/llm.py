"""DashScope Bailian application client (ported from boom_api).

A thin wrapper around the DashScope *Bailian* application API. It is used to
populate the LLM-generated text fields (the coach reply and the ``boom_go_text``
coaching line). Unlike boom_api's LLM layer, there is **no conversation cache**
here: newboom persists coach conversations in the database (``Conversation`` /
``Message``) and stores ``boom_go_text`` on ``DailySummary``, so the filesystem
``ConversationManager`` / ``store`` from boom_api are intentionally not ported.

Config comes from the environment (``load_dotenv`` already runs in
``newboom.database`` / ``newboom.storage``; this module re-runs it defensively so
the client works regardless of import order):

* ``DASHSCOPE_API_KEY``  — the Bailian/DashScope API key.
* ``BAILIAN_APP_ID``     — the Bailian application id.
* ``BAILIAN_WORKSPACE``  — the Bailian workspace (optional).
"""

from __future__ import annotations

import os

from dashscope import Application
from dashscope.app.application_response import ApplicationResponse
from dotenv import load_dotenv

# Load .env at module import time so env vars are available.
load_dotenv()


class Chat:
    """Thin wrapper around DashScope Bailian Application API."""

    def __init__(
        self,
        api_key: str | None = None,
        app_id: str | None = None,
        workspace: str | None = None,
    ) -> None:
        self.api_key = api_key or os.getenv("DASHSCOPE_API_KEY") or ""
        self.app_id = app_id or os.getenv("BAILIAN_APP_ID") or ""
        self.workspace = workspace or os.getenv("BAILIAN_WORKSPACE") or ""

    def ask(self, user_content: str, system_prompt: str = "") -> str | None:
        """Send *user_content* to the Bailian app and return its text reply.

        Returns ``None`` when the app cannot produce output.
        """
        if not user_content or not user_content.strip():
            return None

        kwargs: dict = {
            "api_key": self.api_key,
            "app_id": self.app_id,
            "prompt": user_content,
            "stream": True,
            "stream_options": {
                "include_intermediate_results": False,
                "include_usage": True,
            },
        }

        if self.workspace:
            kwargs["workspace"] = self.workspace

        res = Application.call(**kwargs)

        # Non-streaming path (single ApplicationResponse)
        if isinstance(res, ApplicationResponse):
            if hasattr(res, "status_code") and res.status_code == 200:
                return getattr(getattr(res, "output", None), "text", None) or None
        else:
            # Streaming path iterates over chunks yielding final text.
            for chunk in res:
                output = getattr(chunk, "output", None)
                if output is None:
                    continue
                finish_reason = getattr(output, "finish_reason", None)
                if finish_reason == "stop":
                    return getattr(output, "text", None) or None

        return None
