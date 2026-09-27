"""Generate TypeScript interfaces for the SDK from contracts/generated/uar.v1.schema.json.

    python scripts/gen_ts_types.py          # writes sdks/typescript/src/types.gen.ts
    python scripts/gen_ts_types.py --check  # CI: fail if stale
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "contracts" / "generated" / "uar.v1.schema.json"
OUT = ROOT / "sdks" / "typescript" / "src" / "types.gen.ts"


def ts_type(s: dict) -> str:
    if "$ref" in s:
        return s["$ref"].rsplit("/", 1)[-1]
    t = s.get("type")
    if isinstance(t, list):
        return " | ".join(ts_type({**s, "type": x}) for x in t)
    if t == "array":
        return f"{ts_type(s['items'])}[]"
    if t == "object":
        if "additionalProperties" in s and isinstance(s["additionalProperties"], dict):
            return f"Record<string, {ts_type(s['additionalProperties'])}>"
        return "Record<string, unknown>"
    if "enum" in s:
        return " | ".join(json.dumps(v) for v in s["enum"])
    return {"string": "string", "integer": "number", "number": "number", "boolean": "boolean"}.get(t, "unknown")


def render() -> str:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    out = ["// Generated from proto/uarpb/v1/runtime.proto via contracts/generated/uar.v1.schema.json.",
           "// Do not edit: run `python scripts/gen_ts_types.py`.", ""]
    for name, d in schema["$defs"].items():
        if d.get("description"):
            out.append(f"/** {d['description'].strip()} */")
        out.append(f"export interface {name} {{")
        for field, fs in d["properties"].items():
            doc = f"  /** {fs['description'].strip()} */\n" if fs.get("description") else ""
            out.append(f"{doc}  {field}?: {ts_type(fs)};")
        out.append("}")
        out.append("")
    return "\n".join(out)


def main() -> None:
    text = render()
    if "--check" in sys.argv:
        if not OUT.exists() or OUT.read_text(encoding="utf-8") != text:
            sys.exit("sdks/typescript/src/types.gen.ts is stale")
        print("types.gen.ts up to date")
        return
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
