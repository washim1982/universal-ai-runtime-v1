"""Proto-backed validation shared by all transports.

Requests: JSON is validated by parsing into the proto message (unknown fields rejected), but the
original JSON is handed to the service so integers stay integers (Struct stores all numbers as doubles).
Responses: every response dict is parsed into its proto message before it is sent, so a
transport can never emit something outside the contract.
"""
from __future__ import annotations

from typing import Any, Type

from google.protobuf import json_format
from google.protobuf.message import Message

from uarpb.v1 import runtime_pb2 as pb

from ..errors import UARError

EVENT_BODIES = [f.name for f in pb.Event.DESCRIPTOR.oneofs_by_name["body"].fields]


def validate_request(cls: Type[Message], data: Any) -> dict:
    if not isinstance(data, dict):
        raise UARError("invalid_argument", "request body must be a JSON object")
    try:
        json_format.ParseDict(data, cls(), ignore_unknown_fields=False)
    except json_format.ParseError as e:
        raise UARError("invalid_argument", f"invalid {cls.DESCRIPTOR.name}: {str(e)[:300]}") from None
    return data


def to_proto(cls: Type[Message], data: dict) -> Message:
    try:
        return json_format.ParseDict(data, cls(), ignore_unknown_fields=False)
    except json_format.ParseError as e:  # a contract violation inside the runtime
        raise UARError("internal", f"response violates {cls.DESCRIPTOR.name} contract",
                       details={"error": str(e)[:300]}) from None


def check_response(cls: Type[Message], data: dict) -> dict:
    to_proto(cls, data)
    return data


def event_json(ev: dict) -> dict:
    """Validate an Event dict and add the JSON-only "type" discriminator."""
    check = {k: v for k, v in ev.items() if k != "type"}
    to_proto(pb.Event, check)
    kind = next((k for k in EVENT_BODIES if k in ev), "unknown")
    return {"type": kind, **check}


def from_proto(msg: Message) -> dict:
    return normalize_numbers(json_format.MessageToDict(msg, preserving_proto_field_name=True))


def normalize_numbers(v: Any) -> Any:
    """Struct carries every number as a double; restore integral values to int."""
    if isinstance(v, float) and v.is_integer() and abs(v) < 2 ** 53:
        return int(v)
    if isinstance(v, dict):
        return {k: normalize_numbers(x) for k, x in v.items()}
    if isinstance(v, list):
        return [normalize_numbers(x) for x in v]
    return v
