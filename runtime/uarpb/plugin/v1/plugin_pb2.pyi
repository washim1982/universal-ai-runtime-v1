from google.protobuf import struct_pb2 as _struct_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class DescribeRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class PluginDescriptor(_message.Message):
    __slots__ = ("id", "version", "kind", "api", "capabilities", "config_schema", "input_schema", "output_schema", "tools", "models")
    ID_FIELD_NUMBER: _ClassVar[int]
    VERSION_FIELD_NUMBER: _ClassVar[int]
    KIND_FIELD_NUMBER: _ClassVar[int]
    API_FIELD_NUMBER: _ClassVar[int]
    CAPABILITIES_FIELD_NUMBER: _ClassVar[int]
    CONFIG_SCHEMA_FIELD_NUMBER: _ClassVar[int]
    INPUT_SCHEMA_FIELD_NUMBER: _ClassVar[int]
    OUTPUT_SCHEMA_FIELD_NUMBER: _ClassVar[int]
    TOOLS_FIELD_NUMBER: _ClassVar[int]
    MODELS_FIELD_NUMBER: _ClassVar[int]
    id: str
    version: str
    kind: str
    api: str
    capabilities: _containers.RepeatedScalarFieldContainer[str]
    config_schema: _struct_pb2.Struct
    input_schema: _struct_pb2.Struct
    output_schema: _struct_pb2.Struct
    tools: _containers.RepeatedCompositeFieldContainer[ToolDescriptor]
    models: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, id: _Optional[str] = ..., version: _Optional[str] = ..., kind: _Optional[str] = ..., api: _Optional[str] = ..., capabilities: _Optional[_Iterable[str]] = ..., config_schema: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., input_schema: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., output_schema: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., tools: _Optional[_Iterable[_Union[ToolDescriptor, _Mapping]]] = ..., models: _Optional[_Iterable[str]] = ...) -> None: ...

class ToolDescriptor(_message.Message):
    __slots__ = ("name", "description", "input_schema", "side_effect")
    NAME_FIELD_NUMBER: _ClassVar[int]
    DESCRIPTION_FIELD_NUMBER: _ClassVar[int]
    INPUT_SCHEMA_FIELD_NUMBER: _ClassVar[int]
    SIDE_EFFECT_FIELD_NUMBER: _ClassVar[int]
    name: str
    description: str
    input_schema: _struct_pb2.Struct
    side_effect: str
    def __init__(self, name: _Optional[str] = ..., description: _Optional[str] = ..., input_schema: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., side_effect: _Optional[str] = ...) -> None: ...

class PluginInitRequest(_message.Message):
    __slots__ = ("config", "secrets")
    class SecretsEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    CONFIG_FIELD_NUMBER: _ClassVar[int]
    SECRETS_FIELD_NUMBER: _ClassVar[int]
    config: _struct_pb2.Struct
    secrets: _containers.ScalarMap[str, str]
    def __init__(self, config: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., secrets: _Optional[_Mapping[str, str]] = ...) -> None: ...

class PluginInitResponse(_message.Message):
    __slots__ = ("ok", "message")
    OK_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    ok: bool
    message: str
    def __init__(self, ok: _Optional[bool] = ..., message: _Optional[str] = ...) -> None: ...

class ExecutionContext(_message.Message):
    __slots__ = ("tenant", "run_id", "request_id", "deadline_unix_ms", "idempotency_key", "capability_token", "traceparent")
    TENANT_FIELD_NUMBER: _ClassVar[int]
    RUN_ID_FIELD_NUMBER: _ClassVar[int]
    REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    DEADLINE_UNIX_MS_FIELD_NUMBER: _ClassVar[int]
    IDEMPOTENCY_KEY_FIELD_NUMBER: _ClassVar[int]
    CAPABILITY_TOKEN_FIELD_NUMBER: _ClassVar[int]
    TRACEPARENT_FIELD_NUMBER: _ClassVar[int]
    tenant: str
    run_id: str
    request_id: str
    deadline_unix_ms: int
    idempotency_key: str
    capability_token: str
    traceparent: str
    def __init__(self, tenant: _Optional[str] = ..., run_id: _Optional[str] = ..., request_id: _Optional[str] = ..., deadline_unix_ms: _Optional[int] = ..., idempotency_key: _Optional[str] = ..., capability_token: _Optional[str] = ..., traceparent: _Optional[str] = ...) -> None: ...

class ModelCall(_message.Message):
    __slots__ = ("model", "messages", "params", "tools")
    MODEL_FIELD_NUMBER: _ClassVar[int]
    MESSAGES_FIELD_NUMBER: _ClassVar[int]
    PARAMS_FIELD_NUMBER: _ClassVar[int]
    TOOLS_FIELD_NUMBER: _ClassVar[int]
    model: str
    messages: _containers.RepeatedCompositeFieldContainer[_struct_pb2.Struct]
    params: _struct_pb2.Struct
    tools: _containers.RepeatedCompositeFieldContainer[_struct_pb2.Struct]
    def __init__(self, model: _Optional[str] = ..., messages: _Optional[_Iterable[_Union[_struct_pb2.Struct, _Mapping]]] = ..., params: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., tools: _Optional[_Iterable[_Union[_struct_pb2.Struct, _Mapping]]] = ...) -> None: ...

class ToolCall(_message.Message):
    __slots__ = ("tool", "args")
    TOOL_FIELD_NUMBER: _ClassVar[int]
    ARGS_FIELD_NUMBER: _ClassVar[int]
    tool: str
    args: _struct_pb2.Struct
    def __init__(self, tool: _Optional[str] = ..., args: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class AgentCall(_message.Message):
    __slots__ = ("agent", "input")
    AGENT_FIELD_NUMBER: _ClassVar[int]
    INPUT_FIELD_NUMBER: _ClassVar[int]
    agent: str
    input: _struct_pb2.Struct
    def __init__(self, agent: _Optional[str] = ..., input: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class PluginExecuteRequest(_message.Message):
    __slots__ = ("ctx", "model", "tool", "agent")
    CTX_FIELD_NUMBER: _ClassVar[int]
    MODEL_FIELD_NUMBER: _ClassVar[int]
    TOOL_FIELD_NUMBER: _ClassVar[int]
    AGENT_FIELD_NUMBER: _ClassVar[int]
    ctx: ExecutionContext
    model: ModelCall
    tool: ToolCall
    agent: AgentCall
    def __init__(self, ctx: _Optional[_Union[ExecutionContext, _Mapping]] = ..., model: _Optional[_Union[ModelCall, _Mapping]] = ..., tool: _Optional[_Union[ToolCall, _Mapping]] = ..., agent: _Optional[_Union[AgentCall, _Mapping]] = ...) -> None: ...

class PluginExecuteResponse(_message.Message):
    __slots__ = ("is_error", "error_code", "error_message", "text", "output", "input_tokens", "output_tokens", "finish_reason")
    IS_ERROR_FIELD_NUMBER: _ClassVar[int]
    ERROR_CODE_FIELD_NUMBER: _ClassVar[int]
    ERROR_MESSAGE_FIELD_NUMBER: _ClassVar[int]
    TEXT_FIELD_NUMBER: _ClassVar[int]
    OUTPUT_FIELD_NUMBER: _ClassVar[int]
    INPUT_TOKENS_FIELD_NUMBER: _ClassVar[int]
    OUTPUT_TOKENS_FIELD_NUMBER: _ClassVar[int]
    FINISH_REASON_FIELD_NUMBER: _ClassVar[int]
    is_error: bool
    error_code: str
    error_message: str
    text: str
    output: _struct_pb2.Struct
    input_tokens: int
    output_tokens: int
    finish_reason: str
    def __init__(self, is_error: _Optional[bool] = ..., error_code: _Optional[str] = ..., error_message: _Optional[str] = ..., text: _Optional[str] = ..., output: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., input_tokens: _Optional[int] = ..., output_tokens: _Optional[int] = ..., finish_reason: _Optional[str] = ...) -> None: ...

class PluginEvent(_message.Message):
    __slots__ = ("token", "final")
    TOKEN_FIELD_NUMBER: _ClassVar[int]
    FINAL_FIELD_NUMBER: _ClassVar[int]
    token: str
    final: PluginExecuteResponse
    def __init__(self, token: _Optional[str] = ..., final: _Optional[_Union[PluginExecuteResponse, _Mapping]] = ...) -> None: ...

class HealthRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class HealthResponse(_message.Message):
    __slots__ = ("serving", "message")
    SERVING_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    serving: bool
    message: str
    def __init__(self, serving: _Optional[bool] = ..., message: _Optional[str] = ...) -> None: ...

class ShutdownRequest(_message.Message):
    __slots__ = ("grace_ms",)
    GRACE_MS_FIELD_NUMBER: _ClassVar[int]
    grace_ms: int
    def __init__(self, grace_ms: _Optional[int] = ...) -> None: ...

class ShutdownResponse(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...
