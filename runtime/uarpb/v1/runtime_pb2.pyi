import datetime

from google.protobuf import struct_pb2 as _struct_pb2
from google.protobuf import timestamp_pb2 as _timestamp_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class Money(_message.Message):
    __slots__ = ("amount", "currency")
    AMOUNT_FIELD_NUMBER: _ClassVar[int]
    CURRENCY_FIELD_NUMBER: _ClassVar[int]
    amount: str
    currency: str
    def __init__(self, amount: _Optional[str] = ..., currency: _Optional[str] = ...) -> None: ...

class Usage(_message.Message):
    __slots__ = ("input_tokens", "output_tokens", "cost", "estimated", "price_version")
    INPUT_TOKENS_FIELD_NUMBER: _ClassVar[int]
    OUTPUT_TOKENS_FIELD_NUMBER: _ClassVar[int]
    COST_FIELD_NUMBER: _ClassVar[int]
    ESTIMATED_FIELD_NUMBER: _ClassVar[int]
    PRICE_VERSION_FIELD_NUMBER: _ClassVar[int]
    input_tokens: int
    output_tokens: int
    cost: Money
    estimated: bool
    price_version: str
    def __init__(self, input_tokens: _Optional[int] = ..., output_tokens: _Optional[int] = ..., cost: _Optional[_Union[Money, _Mapping]] = ..., estimated: _Optional[bool] = ..., price_version: _Optional[str] = ...) -> None: ...

class Error(_message.Message):
    __slots__ = ("code", "message", "request_id", "retryable", "details")
    CODE_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    RETRYABLE_FIELD_NUMBER: _ClassVar[int]
    DETAILS_FIELD_NUMBER: _ClassVar[int]
    code: str
    message: str
    request_id: str
    retryable: bool
    details: _struct_pb2.Struct
    def __init__(self, code: _Optional[str] = ..., message: _Optional[str] = ..., request_id: _Optional[str] = ..., retryable: _Optional[bool] = ..., details: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class ToolCallRequest(_message.Message):
    __slots__ = ("id", "name", "args")
    ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    ARGS_FIELD_NUMBER: _ClassVar[int]
    id: str
    name: str
    args: _struct_pb2.Struct
    def __init__(self, id: _Optional[str] = ..., name: _Optional[str] = ..., args: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class ChatMessage(_message.Message):
    __slots__ = ("role", "content", "tool_calls", "tool_call_id")
    ROLE_FIELD_NUMBER: _ClassVar[int]
    CONTENT_FIELD_NUMBER: _ClassVar[int]
    TOOL_CALLS_FIELD_NUMBER: _ClassVar[int]
    TOOL_CALL_ID_FIELD_NUMBER: _ClassVar[int]
    role: str
    content: str
    tool_calls: _containers.RepeatedCompositeFieldContainer[ToolCallRequest]
    tool_call_id: str
    def __init__(self, role: _Optional[str] = ..., content: _Optional[str] = ..., tool_calls: _Optional[_Iterable[_Union[ToolCallRequest, _Mapping]]] = ..., tool_call_id: _Optional[str] = ...) -> None: ...

class GenerationParams(_message.Message):
    __slots__ = ("temperature", "max_tokens", "top_p", "stop", "response_schema")
    TEMPERATURE_FIELD_NUMBER: _ClassVar[int]
    MAX_TOKENS_FIELD_NUMBER: _ClassVar[int]
    TOP_P_FIELD_NUMBER: _ClassVar[int]
    STOP_FIELD_NUMBER: _ClassVar[int]
    RESPONSE_SCHEMA_FIELD_NUMBER: _ClassVar[int]
    temperature: float
    max_tokens: int
    top_p: float
    stop: _containers.RepeatedScalarFieldContainer[str]
    response_schema: _struct_pb2.Struct
    def __init__(self, temperature: _Optional[float] = ..., max_tokens: _Optional[int] = ..., top_p: _Optional[float] = ..., stop: _Optional[_Iterable[str]] = ..., response_schema: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class InferenceRequest(_message.Message):
    __slots__ = ("model", "messages", "input", "tools", "tool_mode", "agent", "stream", "params", "data_class", "extensions")
    MODEL_FIELD_NUMBER: _ClassVar[int]
    MESSAGES_FIELD_NUMBER: _ClassVar[int]
    INPUT_FIELD_NUMBER: _ClassVar[int]
    TOOLS_FIELD_NUMBER: _ClassVar[int]
    TOOL_MODE_FIELD_NUMBER: _ClassVar[int]
    AGENT_FIELD_NUMBER: _ClassVar[int]
    STREAM_FIELD_NUMBER: _ClassVar[int]
    PARAMS_FIELD_NUMBER: _ClassVar[int]
    DATA_CLASS_FIELD_NUMBER: _ClassVar[int]
    EXTENSIONS_FIELD_NUMBER: _ClassVar[int]
    model: str
    messages: _containers.RepeatedCompositeFieldContainer[ChatMessage]
    input: str
    tools: _containers.RepeatedScalarFieldContainer[str]
    tool_mode: str
    agent: str
    stream: bool
    params: GenerationParams
    data_class: str
    extensions: _struct_pb2.Struct
    def __init__(self, model: _Optional[str] = ..., messages: _Optional[_Iterable[_Union[ChatMessage, _Mapping]]] = ..., input: _Optional[str] = ..., tools: _Optional[_Iterable[str]] = ..., tool_mode: _Optional[str] = ..., agent: _Optional[str] = ..., stream: _Optional[bool] = ..., params: _Optional[_Union[GenerationParams, _Mapping]] = ..., data_class: _Optional[str] = ..., extensions: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class InferenceResponse(_message.Message):
    __slots__ = ("request_id", "provider", "model", "content", "finish_reason", "tool_calls", "usage", "run_id", "output", "route")
    REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    PROVIDER_FIELD_NUMBER: _ClassVar[int]
    MODEL_FIELD_NUMBER: _ClassVar[int]
    CONTENT_FIELD_NUMBER: _ClassVar[int]
    FINISH_REASON_FIELD_NUMBER: _ClassVar[int]
    TOOL_CALLS_FIELD_NUMBER: _ClassVar[int]
    USAGE_FIELD_NUMBER: _ClassVar[int]
    RUN_ID_FIELD_NUMBER: _ClassVar[int]
    OUTPUT_FIELD_NUMBER: _ClassVar[int]
    ROUTE_FIELD_NUMBER: _ClassVar[int]
    request_id: str
    provider: str
    model: str
    content: str
    finish_reason: str
    tool_calls: _containers.RepeatedCompositeFieldContainer[ToolCallRequest]
    usage: Usage
    run_id: str
    output: _struct_pb2.Struct
    route: RouteDecision
    def __init__(self, request_id: _Optional[str] = ..., provider: _Optional[str] = ..., model: _Optional[str] = ..., content: _Optional[str] = ..., finish_reason: _Optional[str] = ..., tool_calls: _Optional[_Iterable[_Union[ToolCallRequest, _Mapping]]] = ..., usage: _Optional[_Union[Usage, _Mapping]] = ..., run_id: _Optional[str] = ..., output: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., route: _Optional[_Union[RouteDecision, _Mapping]] = ...) -> None: ...

class RouteDecision(_message.Message):
    __slots__ = ("requested", "model_class", "provider", "model", "reasons", "fallback_used")
    REQUESTED_FIELD_NUMBER: _ClassVar[int]
    MODEL_CLASS_FIELD_NUMBER: _ClassVar[int]
    PROVIDER_FIELD_NUMBER: _ClassVar[int]
    MODEL_FIELD_NUMBER: _ClassVar[int]
    REASONS_FIELD_NUMBER: _ClassVar[int]
    FALLBACK_USED_FIELD_NUMBER: _ClassVar[int]
    requested: str
    model_class: str
    provider: str
    model: str
    reasons: _containers.RepeatedScalarFieldContainer[str]
    fallback_used: bool
    def __init__(self, requested: _Optional[str] = ..., model_class: _Optional[str] = ..., provider: _Optional[str] = ..., model: _Optional[str] = ..., reasons: _Optional[_Iterable[str]] = ..., fallback_used: _Optional[bool] = ...) -> None: ...

class ToolRequest(_message.Message):
    __slots__ = ("tool", "args", "idempotency_key")
    TOOL_FIELD_NUMBER: _ClassVar[int]
    ARGS_FIELD_NUMBER: _ClassVar[int]
    IDEMPOTENCY_KEY_FIELD_NUMBER: _ClassVar[int]
    tool: str
    args: _struct_pb2.Struct
    idempotency_key: str
    def __init__(self, tool: _Optional[str] = ..., args: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., idempotency_key: _Optional[str] = ...) -> None: ...

class ContentBlock(_message.Message):
    __slots__ = ("type", "text", "json")
    TYPE_FIELD_NUMBER: _ClassVar[int]
    TEXT_FIELD_NUMBER: _ClassVar[int]
    JSON_FIELD_NUMBER: _ClassVar[int]
    type: str
    text: str
    json: _struct_pb2.Struct
    def __init__(self, type: _Optional[str] = ..., text: _Optional[str] = ..., json: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class ToolResult(_message.Message):
    __slots__ = ("request_id", "tool", "is_error", "content", "structured", "truncated", "untrusted", "duration_ms")
    REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    TOOL_FIELD_NUMBER: _ClassVar[int]
    IS_ERROR_FIELD_NUMBER: _ClassVar[int]
    CONTENT_FIELD_NUMBER: _ClassVar[int]
    STRUCTURED_FIELD_NUMBER: _ClassVar[int]
    TRUNCATED_FIELD_NUMBER: _ClassVar[int]
    UNTRUSTED_FIELD_NUMBER: _ClassVar[int]
    DURATION_MS_FIELD_NUMBER: _ClassVar[int]
    request_id: str
    tool: str
    is_error: bool
    content: _containers.RepeatedCompositeFieldContainer[ContentBlock]
    structured: _struct_pb2.Struct
    truncated: bool
    untrusted: bool
    duration_ms: int
    def __init__(self, request_id: _Optional[str] = ..., tool: _Optional[str] = ..., is_error: _Optional[bool] = ..., content: _Optional[_Iterable[_Union[ContentBlock, _Mapping]]] = ..., structured: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., truncated: _Optional[bool] = ..., untrusted: _Optional[bool] = ..., duration_ms: _Optional[int] = ...) -> None: ...

class ToolInfo(_message.Message):
    __slots__ = ("name", "server", "description", "input_schema", "side_effect")
    NAME_FIELD_NUMBER: _ClassVar[int]
    SERVER_FIELD_NUMBER: _ClassVar[int]
    DESCRIPTION_FIELD_NUMBER: _ClassVar[int]
    INPUT_SCHEMA_FIELD_NUMBER: _ClassVar[int]
    SIDE_EFFECT_FIELD_NUMBER: _ClassVar[int]
    name: str
    server: str
    description: str
    input_schema: _struct_pb2.Struct
    side_effect: str
    def __init__(self, name: _Optional[str] = ..., server: _Optional[str] = ..., description: _Optional[str] = ..., input_schema: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., side_effect: _Optional[str] = ...) -> None: ...

class ListToolsRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class ListToolsResponse(_message.Message):
    __slots__ = ("tools",)
    TOOLS_FIELD_NUMBER: _ClassVar[int]
    tools: _containers.RepeatedCompositeFieldContainer[ToolInfo]
    def __init__(self, tools: _Optional[_Iterable[_Union[ToolInfo, _Mapping]]] = ...) -> None: ...

class ModelInfo(_message.Message):
    __slots__ = ("name", "model_class", "provider", "model", "capabilities", "available")
    NAME_FIELD_NUMBER: _ClassVar[int]
    MODEL_CLASS_FIELD_NUMBER: _ClassVar[int]
    PROVIDER_FIELD_NUMBER: _ClassVar[int]
    MODEL_FIELD_NUMBER: _ClassVar[int]
    CAPABILITIES_FIELD_NUMBER: _ClassVar[int]
    AVAILABLE_FIELD_NUMBER: _ClassVar[int]
    name: str
    model_class: str
    provider: str
    model: str
    capabilities: _containers.RepeatedScalarFieldContainer[str]
    available: bool
    def __init__(self, name: _Optional[str] = ..., model_class: _Optional[str] = ..., provider: _Optional[str] = ..., model: _Optional[str] = ..., capabilities: _Optional[_Iterable[str]] = ..., available: _Optional[bool] = ...) -> None: ...

class ListModelsRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class ListModelsResponse(_message.Message):
    __slots__ = ("models",)
    MODELS_FIELD_NUMBER: _ClassVar[int]
    models: _containers.RepeatedCompositeFieldContainer[ModelInfo]
    def __init__(self, models: _Optional[_Iterable[_Union[ModelInfo, _Mapping]]] = ...) -> None: ...

class RegisterAgentRequest(_message.Message):
    __slots__ = ("definition",)
    DEFINITION_FIELD_NUMBER: _ClassVar[int]
    definition: _struct_pb2.Struct
    def __init__(self, definition: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class AgentVersion(_message.Message):
    __slots__ = ("agent_id", "version", "digest", "created_at", "warnings")
    AGENT_ID_FIELD_NUMBER: _ClassVar[int]
    VERSION_FIELD_NUMBER: _ClassVar[int]
    DIGEST_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    WARNINGS_FIELD_NUMBER: _ClassVar[int]
    agent_id: str
    version: str
    digest: str
    created_at: _timestamp_pb2.Timestamp
    warnings: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, agent_id: _Optional[str] = ..., version: _Optional[str] = ..., digest: _Optional[str] = ..., created_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., warnings: _Optional[_Iterable[str]] = ...) -> None: ...

class RunRequest(_message.Message):
    __slots__ = ("agent_id", "version", "input", "idempotency_key")
    AGENT_ID_FIELD_NUMBER: _ClassVar[int]
    VERSION_FIELD_NUMBER: _ClassVar[int]
    INPUT_FIELD_NUMBER: _ClassVar[int]
    IDEMPOTENCY_KEY_FIELD_NUMBER: _ClassVar[int]
    agent_id: str
    version: str
    input: _struct_pb2.Struct
    idempotency_key: str
    def __init__(self, agent_id: _Optional[str] = ..., version: _Optional[str] = ..., input: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., idempotency_key: _Optional[str] = ...) -> None: ...

class Run(_message.Message):
    __slots__ = ("run_id", "agent_id", "version", "status", "output", "error", "usage", "steps", "current_node", "created_at", "updated_at", "parent_run_id", "cancel_requested")
    RUN_ID_FIELD_NUMBER: _ClassVar[int]
    AGENT_ID_FIELD_NUMBER: _ClassVar[int]
    VERSION_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    OUTPUT_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    USAGE_FIELD_NUMBER: _ClassVar[int]
    STEPS_FIELD_NUMBER: _ClassVar[int]
    CURRENT_NODE_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    UPDATED_AT_FIELD_NUMBER: _ClassVar[int]
    PARENT_RUN_ID_FIELD_NUMBER: _ClassVar[int]
    CANCEL_REQUESTED_FIELD_NUMBER: _ClassVar[int]
    run_id: str
    agent_id: str
    version: str
    status: str
    output: _struct_pb2.Struct
    error: Error
    usage: Usage
    steps: int
    current_node: str
    created_at: _timestamp_pb2.Timestamp
    updated_at: _timestamp_pb2.Timestamp
    parent_run_id: str
    cancel_requested: bool
    def __init__(self, run_id: _Optional[str] = ..., agent_id: _Optional[str] = ..., version: _Optional[str] = ..., status: _Optional[str] = ..., output: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., error: _Optional[_Union[Error, _Mapping]] = ..., usage: _Optional[_Union[Usage, _Mapping]] = ..., steps: _Optional[int] = ..., current_node: _Optional[str] = ..., created_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., updated_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., parent_run_id: _Optional[str] = ..., cancel_requested: _Optional[bool] = ...) -> None: ...

class GetRunRequest(_message.Message):
    __slots__ = ("run_id",)
    RUN_ID_FIELD_NUMBER: _ClassVar[int]
    run_id: str
    def __init__(self, run_id: _Optional[str] = ...) -> None: ...

class WatchRunRequest(_message.Message):
    __slots__ = ("run_id", "after_seq")
    RUN_ID_FIELD_NUMBER: _ClassVar[int]
    AFTER_SEQ_FIELD_NUMBER: _ClassVar[int]
    run_id: str
    after_seq: int
    def __init__(self, run_id: _Optional[str] = ..., after_seq: _Optional[int] = ...) -> None: ...

class CancelRunRequest(_message.Message):
    __slots__ = ("run_id", "reason")
    RUN_ID_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    run_id: str
    reason: str
    def __init__(self, run_id: _Optional[str] = ..., reason: _Optional[str] = ...) -> None: ...

class ResolveRunRequest(_message.Message):
    __slots__ = ("run_id", "action", "note")
    RUN_ID_FIELD_NUMBER: _ClassVar[int]
    ACTION_FIELD_NUMBER: _ClassVar[int]
    NOTE_FIELD_NUMBER: _ClassVar[int]
    run_id: str
    action: str
    note: str
    def __init__(self, run_id: _Optional[str] = ..., action: _Optional[str] = ..., note: _Optional[str] = ...) -> None: ...

class Started(_message.Message):
    __slots__ = ("model", "provider", "agent_id")
    MODEL_FIELD_NUMBER: _ClassVar[int]
    PROVIDER_FIELD_NUMBER: _ClassVar[int]
    AGENT_ID_FIELD_NUMBER: _ClassVar[int]
    model: str
    provider: str
    agent_id: str
    def __init__(self, model: _Optional[str] = ..., provider: _Optional[str] = ..., agent_id: _Optional[str] = ...) -> None: ...

class TokenDelta(_message.Message):
    __slots__ = ("text",)
    TEXT_FIELD_NUMBER: _ClassVar[int]
    text: str
    def __init__(self, text: _Optional[str] = ...) -> None: ...

class ToolCallEvent(_message.Message):
    __slots__ = ("id", "tool", "args", "node_id")
    ID_FIELD_NUMBER: _ClassVar[int]
    TOOL_FIELD_NUMBER: _ClassVar[int]
    ARGS_FIELD_NUMBER: _ClassVar[int]
    NODE_ID_FIELD_NUMBER: _ClassVar[int]
    id: str
    tool: str
    args: _struct_pb2.Struct
    node_id: str
    def __init__(self, id: _Optional[str] = ..., tool: _Optional[str] = ..., args: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., node_id: _Optional[str] = ...) -> None: ...

class ToolResultEvent(_message.Message):
    __slots__ = ("id", "tool", "is_error", "summary", "node_id")
    ID_FIELD_NUMBER: _ClassVar[int]
    TOOL_FIELD_NUMBER: _ClassVar[int]
    IS_ERROR_FIELD_NUMBER: _ClassVar[int]
    SUMMARY_FIELD_NUMBER: _ClassVar[int]
    NODE_ID_FIELD_NUMBER: _ClassVar[int]
    id: str
    tool: str
    is_error: bool
    summary: str
    node_id: str
    def __init__(self, id: _Optional[str] = ..., tool: _Optional[str] = ..., is_error: _Optional[bool] = ..., summary: _Optional[str] = ..., node_id: _Optional[str] = ...) -> None: ...

class NodeStarted(_message.Message):
    __slots__ = ("node_id", "node_type", "attempt")
    NODE_ID_FIELD_NUMBER: _ClassVar[int]
    NODE_TYPE_FIELD_NUMBER: _ClassVar[int]
    ATTEMPT_FIELD_NUMBER: _ClassVar[int]
    node_id: str
    node_type: str
    attempt: int
    def __init__(self, node_id: _Optional[str] = ..., node_type: _Optional[str] = ..., attempt: _Optional[int] = ...) -> None: ...

class NodeCompleted(_message.Message):
    __slots__ = ("node_id", "outcome", "duration_ms", "next")
    NODE_ID_FIELD_NUMBER: _ClassVar[int]
    OUTCOME_FIELD_NUMBER: _ClassVar[int]
    DURATION_MS_FIELD_NUMBER: _ClassVar[int]
    NEXT_FIELD_NUMBER: _ClassVar[int]
    node_id: str
    outcome: str
    duration_ms: int
    next: str
    def __init__(self, node_id: _Optional[str] = ..., outcome: _Optional[str] = ..., duration_ms: _Optional[int] = ..., next: _Optional[str] = ...) -> None: ...

class ApprovalRequired(_message.Message):
    __slots__ = ("approval_id", "action", "args_hash")
    APPROVAL_ID_FIELD_NUMBER: _ClassVar[int]
    ACTION_FIELD_NUMBER: _ClassVar[int]
    ARGS_HASH_FIELD_NUMBER: _ClassVar[int]
    approval_id: str
    action: str
    args_hash: str
    def __init__(self, approval_id: _Optional[str] = ..., action: _Optional[str] = ..., args_hash: _Optional[str] = ...) -> None: ...

class Completed(_message.Message):
    __slots__ = ("status", "content", "output", "finish_reason")
    STATUS_FIELD_NUMBER: _ClassVar[int]
    CONTENT_FIELD_NUMBER: _ClassVar[int]
    OUTPUT_FIELD_NUMBER: _ClassVar[int]
    FINISH_REASON_FIELD_NUMBER: _ClassVar[int]
    status: str
    content: str
    output: _struct_pb2.Struct
    finish_reason: str
    def __init__(self, status: _Optional[str] = ..., content: _Optional[str] = ..., output: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., finish_reason: _Optional[str] = ...) -> None: ...

class Event(_message.Message):
    __slots__ = ("run_id", "seq", "ts", "trace_id", "request_id", "started", "token", "tool_call", "tool_result", "node_started", "node_completed", "usage", "approval_required", "error", "completed")
    RUN_ID_FIELD_NUMBER: _ClassVar[int]
    SEQ_FIELD_NUMBER: _ClassVar[int]
    TS_FIELD_NUMBER: _ClassVar[int]
    TRACE_ID_FIELD_NUMBER: _ClassVar[int]
    REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    STARTED_FIELD_NUMBER: _ClassVar[int]
    TOKEN_FIELD_NUMBER: _ClassVar[int]
    TOOL_CALL_FIELD_NUMBER: _ClassVar[int]
    TOOL_RESULT_FIELD_NUMBER: _ClassVar[int]
    NODE_STARTED_FIELD_NUMBER: _ClassVar[int]
    NODE_COMPLETED_FIELD_NUMBER: _ClassVar[int]
    USAGE_FIELD_NUMBER: _ClassVar[int]
    APPROVAL_REQUIRED_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    COMPLETED_FIELD_NUMBER: _ClassVar[int]
    run_id: str
    seq: int
    ts: _timestamp_pb2.Timestamp
    trace_id: str
    request_id: str
    started: Started
    token: TokenDelta
    tool_call: ToolCallEvent
    tool_result: ToolResultEvent
    node_started: NodeStarted
    node_completed: NodeCompleted
    usage: Usage
    approval_required: ApprovalRequired
    error: Error
    completed: Completed
    def __init__(self, run_id: _Optional[str] = ..., seq: _Optional[int] = ..., ts: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., trace_id: _Optional[str] = ..., request_id: _Optional[str] = ..., started: _Optional[_Union[Started, _Mapping]] = ..., token: _Optional[_Union[TokenDelta, _Mapping]] = ..., tool_call: _Optional[_Union[ToolCallEvent, _Mapping]] = ..., tool_result: _Optional[_Union[ToolResultEvent, _Mapping]] = ..., node_started: _Optional[_Union[NodeStarted, _Mapping]] = ..., node_completed: _Optional[_Union[NodeCompleted, _Mapping]] = ..., usage: _Optional[_Union[Usage, _Mapping]] = ..., approval_required: _Optional[_Union[ApprovalRequired, _Mapping]] = ..., error: _Optional[_Union[Error, _Mapping]] = ..., completed: _Optional[_Union[Completed, _Mapping]] = ...) -> None: ...

class DryRunRequest(_message.Message):
    __slots__ = ("agent_id", "definition", "inference", "version", "input", "mode", "fixtures", "seed")
    AGENT_ID_FIELD_NUMBER: _ClassVar[int]
    DEFINITION_FIELD_NUMBER: _ClassVar[int]
    INFERENCE_FIELD_NUMBER: _ClassVar[int]
    VERSION_FIELD_NUMBER: _ClassVar[int]
    INPUT_FIELD_NUMBER: _ClassVar[int]
    MODE_FIELD_NUMBER: _ClassVar[int]
    FIXTURES_FIELD_NUMBER: _ClassVar[int]
    SEED_FIELD_NUMBER: _ClassVar[int]
    agent_id: str
    definition: _struct_pb2.Struct
    inference: InferenceRequest
    version: str
    input: _struct_pb2.Struct
    mode: str
    fixtures: _struct_pb2.Struct
    seed: int
    def __init__(self, agent_id: _Optional[str] = ..., definition: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., inference: _Optional[_Union[InferenceRequest, _Mapping]] = ..., version: _Optional[str] = ..., input: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., mode: _Optional[str] = ..., fixtures: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., seed: _Optional[int] = ...) -> None: ...

class PlannedStep(_message.Message):
    __slots__ = ("node_id", "node_type", "detail", "permissions", "status", "output")
    NODE_ID_FIELD_NUMBER: _ClassVar[int]
    NODE_TYPE_FIELD_NUMBER: _ClassVar[int]
    DETAIL_FIELD_NUMBER: _ClassVar[int]
    PERMISSIONS_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    OUTPUT_FIELD_NUMBER: _ClassVar[int]
    node_id: str
    node_type: str
    detail: str
    permissions: _containers.RepeatedScalarFieldContainer[str]
    status: str
    output: _struct_pb2.Struct
    def __init__(self, node_id: _Optional[str] = ..., node_type: _Optional[str] = ..., detail: _Optional[str] = ..., permissions: _Optional[_Iterable[str]] = ..., status: _Optional[str] = ..., output: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class DryRunReport(_message.Message):
    __slots__ = ("request_id", "mode", "valid", "errors", "warnings", "steps", "routes", "permissions_required", "permissions_missing", "unresolved", "branches", "estimated_input_tokens_max", "estimated_output_tokens_max", "estimated_cost_max", "executed_nothing")
    REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    MODE_FIELD_NUMBER: _ClassVar[int]
    VALID_FIELD_NUMBER: _ClassVar[int]
    ERRORS_FIELD_NUMBER: _ClassVar[int]
    WARNINGS_FIELD_NUMBER: _ClassVar[int]
    STEPS_FIELD_NUMBER: _ClassVar[int]
    ROUTES_FIELD_NUMBER: _ClassVar[int]
    PERMISSIONS_REQUIRED_FIELD_NUMBER: _ClassVar[int]
    PERMISSIONS_MISSING_FIELD_NUMBER: _ClassVar[int]
    UNRESOLVED_FIELD_NUMBER: _ClassVar[int]
    BRANCHES_FIELD_NUMBER: _ClassVar[int]
    ESTIMATED_INPUT_TOKENS_MAX_FIELD_NUMBER: _ClassVar[int]
    ESTIMATED_OUTPUT_TOKENS_MAX_FIELD_NUMBER: _ClassVar[int]
    ESTIMATED_COST_MAX_FIELD_NUMBER: _ClassVar[int]
    EXECUTED_NOTHING_FIELD_NUMBER: _ClassVar[int]
    request_id: str
    mode: str
    valid: bool
    errors: _containers.RepeatedScalarFieldContainer[str]
    warnings: _containers.RepeatedScalarFieldContainer[str]
    steps: _containers.RepeatedCompositeFieldContainer[PlannedStep]
    routes: _containers.RepeatedCompositeFieldContainer[RouteDecision]
    permissions_required: _containers.RepeatedScalarFieldContainer[str]
    permissions_missing: _containers.RepeatedScalarFieldContainer[str]
    unresolved: _containers.RepeatedScalarFieldContainer[str]
    branches: _containers.RepeatedScalarFieldContainer[str]
    estimated_input_tokens_max: int
    estimated_output_tokens_max: int
    estimated_cost_max: Money
    executed_nothing: bool
    def __init__(self, request_id: _Optional[str] = ..., mode: _Optional[str] = ..., valid: _Optional[bool] = ..., errors: _Optional[_Iterable[str]] = ..., warnings: _Optional[_Iterable[str]] = ..., steps: _Optional[_Iterable[_Union[PlannedStep, _Mapping]]] = ..., routes: _Optional[_Iterable[_Union[RouteDecision, _Mapping]]] = ..., permissions_required: _Optional[_Iterable[str]] = ..., permissions_missing: _Optional[_Iterable[str]] = ..., unresolved: _Optional[_Iterable[str]] = ..., branches: _Optional[_Iterable[str]] = ..., estimated_input_tokens_max: _Optional[int] = ..., estimated_output_tokens_max: _Optional[int] = ..., estimated_cost_max: _Optional[_Union[Money, _Mapping]] = ..., executed_nothing: _Optional[bool] = ...) -> None: ...

class ApprovalDecision(_message.Message):
    __slots__ = ("approval_id", "approve", "comment")
    APPROVAL_ID_FIELD_NUMBER: _ClassVar[int]
    APPROVE_FIELD_NUMBER: _ClassVar[int]
    COMMENT_FIELD_NUMBER: _ClassVar[int]
    approval_id: str
    approve: bool
    comment: str
    def __init__(self, approval_id: _Optional[str] = ..., approve: _Optional[bool] = ..., comment: _Optional[str] = ...) -> None: ...

class Approval(_message.Message):
    __slots__ = ("approval_id", "status", "run_id")
    APPROVAL_ID_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    RUN_ID_FIELD_NUMBER: _ClassVar[int]
    approval_id: str
    status: str
    run_id: str
    def __init__(self, approval_id: _Optional[str] = ..., status: _Optional[str] = ..., run_id: _Optional[str] = ...) -> None: ...
