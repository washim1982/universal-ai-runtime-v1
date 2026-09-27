"""Wire-compatibility check of the current protos against the committed baseline.

    python scripts/check_breaking.py            # compare
    python scripts/check_breaking.py --update   # accept the current contract as the new baseline

Breaking: removing a service, method, message or field; changing a field's number, type,
label or message type; changing a method's request/response/streaming shape.
Adding fields, messages and methods is compatible.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from google.protobuf import descriptor_pb2

ROOT = Path(__file__).resolve().parents[1]
CURRENT = ROOT / "contracts/generated/descriptor.pb"
BASELINE = ROOT / "contracts/baseline/descriptor.pb"


def index(path: Path) -> dict:
    fds = descriptor_pb2.FileDescriptorSet.FromString(path.read_bytes())
    out: dict = {}
    for f in fds.file:
        if not f.package.startswith("uar."):
            continue

        def walk(prefix: str, msgs) -> None:
            for m in msgs:
                name = f"{prefix}.{m.name}"
                out[("msg", name)] = {fl.name: (fl.number, fl.type, fl.label, fl.type_name) for fl in m.field}
                walk(name, m.nested_type)

        walk(f.package, f.message_type)
        for s in f.service:
            out[("svc", f"{f.package}.{s.name}")] = {
                m.name: (m.input_type, m.output_type, m.client_streaming, m.server_streaming) for m in s.method}
    return out


def compare(old: dict, new: dict) -> list[str]:
    problems = []
    for key, members in old.items():
        if key not in new:
            problems.append(f"removed {key[0]} {key[1]}")
            continue
        for name, shape in members.items():
            if name not in new[key]:
                problems.append(f"removed {key[1]}.{name}")
            elif new[key][name] != shape:
                problems.append(f"changed {key[1]}.{name}: {shape} -> {new[key][name]}")
    return problems


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--update", action="store_true")
    a = ap.parse_args()
    if a.update or not BASELINE.exists():
        BASELINE.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(CURRENT, BASELINE)
        print("baseline updated")
        return
    problems = compare(index(BASELINE), index(CURRENT))
    if problems:
        print("BREAKING CHANGES:\n  " + "\n  ".join(problems))
        sys.exit(1)
    print("no breaking changes")


if __name__ == "__main__":
    main()
