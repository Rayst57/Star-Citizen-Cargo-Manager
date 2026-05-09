"""
LLMThread — calls OpenAI chat.completions with the Giant tool list.

Emits tool_call(name, args) on a tool selection, response_text(text) on
a plain text reply, error(message) on failure.
"""

from __future__ import annotations

import json

from PySide6.QtCore import QThread, Signal

from .system_prompt import build_system_prompt
from .tool_schemas import GIANT_TOOLS


class LLMThread(QThread):
    response_text = Signal(str)
    tool_call     = Signal(str, dict)
    error         = Signal(str)

    def __init__(
        self,
        controller,
        transcript: str,
        history: list[dict] | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self.controller = controller
        self.transcript = transcript
        self.history = history or []

    def run(self) -> None:
        try:
            from openai import OpenAI
        except ImportError:
            self.error.emit("openai package not installed.")
            return

        if not self.controller.api_key:
            self.error.emit("OpenAI API key not set.")
            return

        try:
            client = OpenAI(api_key=self.controller.api_key)
            ctx = self.controller.build_context_snapshot()
            system_prompt = build_system_prompt(ctx)
            messages = [
                {"role": "system", "content": system_prompt},
                *self.history,
                {"role": "user", "content": self.transcript},
            ]
            resp = client.chat.completions.create(
                model=self.controller.settings.get("model"),
                messages=messages,
                tools=GIANT_TOOLS,
                tool_choice="auto",
                temperature=0.1,
                max_tokens=512,
            )
            msg = resp.choices[0].message
            if msg.tool_calls:
                tc = msg.tool_calls[0]
                args = json.loads(tc.function.arguments or "{}")
                self.tool_call.emit(tc.function.name, args)
            else:
                self.response_text.emit(msg.content or "")
        except Exception as e:
            self.error.emit(str(e))
