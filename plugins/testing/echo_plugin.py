"""Configurable test plugin (model + tool) used by runtime/tests/test_plugins.py.

    python plugins/testing/echo_plugin.py --id acme.echo --version 1.0.0 [--kind model|tool]
        [--capability network:evil.example] [--hang]

Model: replies "<tag>: <last user message>" where tag comes from the manifest config.
Tool (namespace chosen by the manifest): `shout` upper-cases text.
"""
from __future__ import annotations

import argparse
import time

from uar_plugin import Plugin, PluginError, serve, tool


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", required=True)
    ap.add_argument("--version", required=True)
    ap.add_argument("--kind", default="model")
    ap.add_argument("--capability", action="append", default=[])
    ap.add_argument("--hang", action="store_true")
    a = ap.parse_args()

    class Echo(Plugin):
        id, version, kind = a.id, a.version, a.kind
        models = ["echo"] if a.kind == "model" else []
        capabilities = a.capability

        def init(self, config, secrets):
            self.tag = config.get("tag", self.version)

        def model(self, call, ctx):
            if a.hang:
                time.sleep(3600)
            last = next((m.get("content", "") for m in reversed(call["messages"]) if m.get("role") == "user"), "")
            reply = f"{self.tag}: {last}"
            words = reply.split(" ")

            def gen():
                for i, w in enumerate(words):
                    yield w if i == 0 else " " + w
                yield {"finish_reason": "stop", "input_tokens": len(last.split()), "output_tokens": len(words)}
            return gen()

        @tool("shout", "Upper-case a text", {"type": "object", "required": ["text"],
                                             "properties": {"text": {"type": "string"}}})
        def shout(self, args, ctx):
            if a.hang:
                time.sleep(3600)
            if not args.get("text"):
                raise PluginError("text is empty")
            return {"output": {"text": args["text"].upper(), "by": f"{self.id}@{self.version}"}}

    serve(Echo())


if __name__ == "__main__":
    main()
