"""Reference executable agent plugin (Python): extractive summary of a text.

Graphs call it like any sub-agent:  {type: agent, agent: summarize_text, input: {text: "..."}}
"""
from __future__ import annotations

import re

from uar_plugin import Plugin, PluginError, serve


class Summarizer(Plugin):
    id, version, kind = "acme.summarizer", "1.0.0", "agent"

    def init(self, config: dict, secrets: dict) -> None:
        self.max_sentences = int(config.get("max_sentences", 2))

    def agent(self, agent: str, input: dict, ctx: dict) -> dict:
        if agent != "summarize_text":
            raise PluginError(f"unknown agent {agent}", "not_found")
        text = str(input.get("text", "")).strip()
        if not text:
            raise PluginError("input.text is required", "invalid_argument")
        sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
        words = re.findall(r"\w+", text)
        return {"output": {"summary": " ".join(sentences[: self.max_sentences]), "sentences": len(sentences),
                           "words": len(words)}}


if __name__ == "__main__":
    serve(Summarizer())
