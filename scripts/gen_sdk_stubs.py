"""Generate gRPC/protobuf stubs for the non-Python SDKs from the canonical protos.

    python scripts/gen_sdk_stubs.py go        # sdks/go/uarv1 and plugin-sdks/go/pluginv1

Needs protoc-gen-go and protoc-gen-go-grpc on PATH (go install ...), which grpc_tools' bundled
protoc invokes. Other languages generate their stubs in their own build (Maven/Gradle, MSBuild via
Grpc.Tools, Cargo build.rs with tonic-build), so their generated code is not committed.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import grpc_tools
from grpc_tools import protoc

ROOT = Path(__file__).resolve().parents[1]
PROTO = ROOT / "proto"
WKT = Path(grpc_tools.__file__).parent / "_proto"
MODULE = "github.com/washim1982/universal-ai-runtime-v1"


def go() -> None:
    gopath = subprocess.run(["go", "env", "GOPATH"], capture_output=True, text=True).stdout.strip()
    os.environ["PATH"] = str(Path(gopath) / "bin") + os.pathsep + os.environ["PATH"]
    for tool in ("protoc-gen-go", "protoc-gen-go-grpc"):
        if not shutil.which(tool):
            sys.exit(f"{tool} not found: go install google.golang.org/protobuf/cmd/protoc-gen-go@latest "
                     "google.golang.org/grpc/cmd/protoc-gen-go-grpc@latest")
    for proto, out in (("uarpb/v1/runtime.proto", "sdks/go/uarv1"), ("uarpb/plugin/v1/plugin.proto", "plugin-sdks/go/pluginv1")):
        target = ROOT / out
        target.mkdir(parents=True, exist_ok=True)
        for f in target.glob("*.pb.go"):
            f.unlink()
        args = ["protoc", f"-I{PROTO}", f"-I{WKT}", f"--go_out={ROOT}", f"--go_opt=module={MODULE}",
                f"--go-grpc_out={ROOT}", f"--go-grpc_opt=module={MODULE}", proto]
        if protoc.main(args) != 0:
            sys.exit(f"protoc failed for {proto}")
        print(f"generated {out}")


if __name__ == "__main__":
    {"go": go}[sys.argv[1] if len(sys.argv) > 1 else "go"]()
