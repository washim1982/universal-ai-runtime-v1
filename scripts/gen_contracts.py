"""Generate code and contract artifacts from the canonical protos.

    python scripts/gen_contracts.py          # regenerate
    python scripts/gen_contracts.py --check  # fail if generated files are stale (CI)

Outputs:
  runtime/uarpb/v1/*_pb2*.py, runtime/uarpb/plugin/v1/*_pb2*.py   Python stubs
  contracts/generated/uar.v1.schema.json                       JSON Schema for every message
  contracts/generated/openapi.json                             HTTP/JSON surface
  contracts/generated/descriptor.pb                            descriptor set (breaking-change baseline)
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

import grpc_tools
from grpc_tools import protoc
from google.protobuf import descriptor_pb2

ROOT = Path(__file__).resolve().parents[1]
PROTO = ROOT / "proto"
FILES = ["uarpb/v1/runtime.proto", "uarpb/plugin/v1/plugin.proto"]
WKT = Path(grpc_tools.__file__).parent / "_proto"

# HTTP routes for the public service. Streaming RPCs are served as SSE.
HTTP = {
    "Infer": ("post", "/api/v1/inference", "InferenceRequest", "InferenceResponse", False),
    "InferStream": ("post", "/api/v1/inference", "InferenceRequest", "Event", True),
    "ExecuteTool": ("post", "/api/v1/tool/execute", "ToolRequest", "ToolResult", False),
    "ListTools": ("get", "/api/v1/tools", None, "ListToolsResponse", False),
    "ListModels": ("get", "/api/v1/models", None, "ListModelsResponse", False),
    "RegisterAgent": ("post", "/api/v1/agents", "RegisterAgentRequest", "AgentVersion", False),
    "StartRun": ("post", "/api/v1/agent/run", "RunRequest", "Run", False),
    "GetRun": ("get", "/api/v1/runs/{run_id}", None, "Run", False),
    "WatchRun": ("get", "/api/v1/runs/{run_id}/events", None, "Event", True),
    "CancelRun": ("post", "/api/v1/runs/{run_id}/cancel", "CancelRunRequest", "Run", False),
    "ResolveRun": ("post", "/api/v1/runs/{run_id}/resolve", "ResolveRunRequest", "Run", False),
    "DryRun": ("post", "/api/v1/dry-run", "DryRunRequest", "DryRunReport", False),
    "DecideApproval": ("post", "/api/v1/approvals/{approval_id}/decision", "ApprovalDecision", "Approval", False),
    "RegisterPlugin": ("post", "/api/v1/plugins", "RegisterPluginRequest", "PluginVersion", False),
    "ListPlugins": ("get", "/api/v1/plugins", None, "ListPluginsResponse", False),
    "ActivatePlugin": ("post", "/api/v1/plugins/{plugin_id}/activate", "ActivatePluginRequest", "PluginVersion", False),
    "RollbackPlugin": ("post", "/api/v1/plugins/{plugin_id}/rollback", "RollbackPluginRequest", "PluginVersion", False),
}

SCALARS = {
    descriptor_pb2.FieldDescriptorProto.TYPE_STRING: {"type": "string"},
    descriptor_pb2.FieldDescriptorProto.TYPE_BOOL: {"type": "boolean"},
    descriptor_pb2.FieldDescriptorProto.TYPE_INT32: {"type": "integer", "format": "int32"},
    descriptor_pb2.FieldDescriptorProto.TYPE_UINT32: {"type": "integer", "minimum": 0},
    # proto3 JSON renders 64-bit integers as strings; accept both.
    descriptor_pb2.FieldDescriptorProto.TYPE_INT64: {"type": ["integer", "string"]},
    descriptor_pb2.FieldDescriptorProto.TYPE_DOUBLE: {"type": "number"},
    descriptor_pb2.FieldDescriptorProto.TYPE_FLOAT: {"type": "number"},
}
WELL_KNOWN = {
    ".google.protobuf.Struct": {"type": "object"},
    ".google.protobuf.Timestamp": {"type": "string", "format": "date-time"},
}


def run_protoc(out_py: Path, descriptor_out: Path) -> None:
    args = [
        "protoc", f"-I{PROTO}", f"-I{WKT}",
        f"--python_out={out_py}", f"--pyi_out={out_py}", f"--grpc_python_out={out_py}",
        f"--descriptor_set_out={descriptor_out}", "--include_imports", "--include_source_info",
        *FILES,
    ]
    if protoc.main(args) != 0:
        sys.exit("protoc failed")


def field_schema(f: descriptor_pb2.FieldDescriptorProto) -> dict:
    if f.type == f.TYPE_MESSAGE:
        s = dict(WELL_KNOWN.get(f.type_name) or {"$ref": "#/$defs/" + f.type_name.rsplit(".", 1)[-1]})
    elif f.type == f.TYPE_ENUM:
        s = {"type": "string"}
    else:
        s = dict(SCALARS[f.type])
    if f.label == f.LABEL_REPEATED:
        if f.type == f.TYPE_MESSAGE and f.type_name.endswith("Entry"):
            return {"type": "object", "additionalProperties": {"type": "string"}}
        return {"type": "array", "items": s}
    return s


def json_schema(fds: descriptor_pb2.FileDescriptorSet, package: str) -> dict:
    defs: dict[str, dict] = {}
    for file in fds.file:
        if file.package != package:
            continue
        comments = {tuple(loc.path): loc.leading_comments.strip() + " " + loc.trailing_comments.strip()
                    for loc in file.source_code_info.location}
        for mi, msg in enumerate(file.message_type):
            props, oneofs = {}, {}
            for fi, f in enumerate(msg.field):
                s = field_schema(f)
                doc = comments.get((4, mi, 2, fi), "").strip()
                if doc:
                    s["description"] = doc
                props[f.name] = s
                if f.HasField("oneof_index") and not f.proto3_optional:
                    oneofs.setdefault(msg.oneof_decl[f.oneof_index].name, []).append(f.name)
            d: dict = {"type": "object", "properties": props, "additionalProperties": False}
            if msg.name == "Event":
                d["properties"]["type"] = {"type": "string", "enum": oneofs["body"],
                                           "description": "Name of the populated body field (JSON/SSE only)."}
            if oneofs:
                d["x-oneof"] = oneofs
                d["allOf"] = [{"not": {"required": [a, b]}}
                              for names in oneofs.values() for i, a in enumerate(names) for b in names[i + 1:]]
            doc = comments.get((4, mi), "").strip()
            if doc:
                d["description"] = doc
            defs[msg.name] = d
    return {"$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": f"https://uar.dev/schemas/{package}.schema.json", "title": package, "$defs": defs}


def openapi(schema: dict) -> dict:
    comps = {k: json.loads(json.dumps(v).replace("#/$defs/", "#/components/schemas/"))
             for k, v in schema["$defs"].items()}
    comps["Error"]["description"] = "Every non-2xx response body is {\"error\": Error}."
    err = {"description": "Error", "content": {"application/json": {"schema": {
        "type": "object", "properties": {"error": {"$ref": "#/components/schemas/Error"}}}}}}
    paths: dict = {}
    for rpc, (method, path, req, resp, stream) in HTTP.items():
        op = paths.setdefault(path, {}).get(method)
        media = "text/event-stream" if stream else "application/json"
        content = {media: {"schema": {"$ref": f"#/components/schemas/{resp}"}}}
        if op:  # Infer + InferStream share one path; "stream": true selects SSE.
            op["responses"]["200"]["content"].update(content)
            op["description"] += " With `stream: true` the response is an SSE stream of Event."
            continue
        op = {"operationId": rpc, "summary": f"uar.v1.Runtime/{rpc}", "description": f"gRPC: uar.v1.Runtime/{rpc}.",
              "security": [{"apiKey": []}, {"bearer": []}],
              "responses": {"200": {"description": "OK", "content": content},
                            **{c: err for c in ("400", "401", "403", "404", "409", "413", "429", "500", "501", "503")}}}
        params = [p.strip("{}") for p in path.split("/") if p.startswith("{")]
        op["parameters"] = [{"name": p, "in": "path", "required": True, "schema": {"type": "string"}} for p in params]
        if rpc == "WatchRun":
            op["parameters"] += [{"name": "after_seq", "in": "query", "schema": {"type": "integer"}},
                                 {"name": "Last-Event-ID", "in": "header", "schema": {"type": "string"}}]
        if req:
            op["requestBody"] = {"required": True, "content": {"application/json": {
                "schema": {"$ref": f"#/components/schemas/{req}"}}}}
        if method == "post":
            op["parameters"].append({"name": "Idempotency-Key", "in": "header", "schema": {"type": "string"}})
        paths[path][method] = op
    return {"openapi": "3.1.0",
            "info": {"title": "Universal AI Runtime", "version": "1.0.0",
                     "description": "Generated from proto/uar/v1/runtime.proto. Do not edit."},
            "paths": paths,
            "components": {"schemas": comps, "securitySchemes": {
                "apiKey": {"type": "apiKey", "in": "header", "name": "X-API-Key"},
                "bearer": {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"}}}}


def generate(out_root: Path) -> None:
    py_out = out_root / "runtime"
    gen = out_root / "contracts" / "generated"
    py_out.mkdir(parents=True, exist_ok=True)
    gen.mkdir(parents=True, exist_ok=True)
    desc = gen / "descriptor.pb"
    run_protoc(py_out, desc)
    for pkg in ("uarpb", "uarpb/v1", "uarpb/plugin", "uarpb/plugin/v1"):
        (py_out / pkg / "__init__.py").touch()
    fds = descriptor_pb2.FileDescriptorSet.FromString(desc.read_bytes())
    for f in fds.file:  # source info makes the descriptor noisy; keep the baseline stable
        f.ClearField("source_code_info")
    schema = json_schema(descriptor_pb2.FileDescriptorSet.FromString(desc.read_bytes()), "uar.v1")
    desc.write_bytes(fds.SerializeToString(deterministic=True))
    (gen / "uar.v1.schema.json").write_text(json.dumps(schema, indent=2) + "\n", encoding="utf-8")
    (gen / "openapi.json").write_text(json.dumps(openapi(schema), indent=2) + "\n", encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    if not a.check:
        generate(ROOT)
        print("generated")
        return
    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp)
        generate(t)
        stale = []
        for rel in ["contracts/generated/uar.v1.schema.json", "contracts/generated/openapi.json",
                    "runtime/uarpb/v1/runtime_pb2.py", "runtime/uarpb/plugin/v1/plugin_pb2.py"]:
            if (t / rel).read_bytes() != (ROOT / rel).read_bytes():
                stale.append(rel)
        if stale:
            sys.exit("stale generated files (run scripts/gen_contracts.py): " + ", ".join(stale))
        print("generated files up to date")


if __name__ == "__main__":
    main()
