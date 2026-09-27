// Generated from proto/uarpb/v1/runtime.proto via contracts/generated/uar.v1.schema.json.
// Do not edit: run `python scripts/gen_ts_types.py`.

export interface Money {
  /** decimal string, e.g. "0.0012" */
  amount?: string;
  /** ISO 4217 */
  currency?: string;
}

export interface Usage {
  input_tokens?: number;
  output_tokens?: number;
  /** Absent when no price is known for the model. Unknown is never reported as zero. */
  cost?: Money;
  /** True when counts or cost are estimates rather than provider-reported values. */
  estimated?: boolean;
  price_version?: string;
}

export interface Error {
  /** stable machine code, see contracts/errors.md */
  code?: string;
  /** safe for display */
  message?: string;
  request_id?: string;
  retryable?: boolean;
  /** redacted */
  details?: Record<string, unknown>;
}

export interface ToolCallRequest {
  id?: string;
  name?: string;
  args?: Record<string, unknown>;
}

export interface ChatMessage {
  /** system | user | assistant | tool */
  role?: string;
  content?: string;
  /** assistant messages */
  tool_calls?: ToolCallRequest[];
  /** tool messages */
  tool_call_id?: string;
}

export interface GenerationParams {
  temperature?: number;
  max_tokens?: number;
  top_p?: number;
  stop?: string[];
  /** Request structured output matching this JSON Schema. */
  response_schema?: Record<string, unknown>;
}

export interface InferenceRequest {
  /** [class ":"] [provider "/"] name   e.g. local:llama3:8b, cloud:default, enterprise:corp/finance-7b */
  model?: string;
  messages?: ChatMessage[];
  /** Shorthand for a single user message; ignored when messages is non-empty. */
  input?: string;
  /** Registered tool names exposed to the model for function calling. */
  tools?: string[];
  /** "suggest" (default) | "auto" */
  tool_mode?: string;
  /** When set, runs this agent with {"prompt": <input>} and returns its result. */
  agent?: string;
  stream?: boolean;
  params?: GenerationParams;
  /** public | internal | confidential (policy input) */
  data_class?: string;
  /** Provider-specific extensions, namespaced by provider id, e.g. {"ollama": {"keep_alive": "5m"}}. */
  extensions?: Record<string, unknown>;
}

export interface InferenceResponse {
  request_id?: string;
  provider?: string;
  /** resolved provider model id */
  model?: string;
  content?: string;
  /** stop | length | tool_calls | cancelled | error */
  finish_reason?: string;
  tool_calls?: ToolCallRequest[];
  usage?: Usage;
  /** set when the call ran an agent or the auto tool loop */
  run_id?: string;
  /** agent structured output, if any */
  output?: Record<string, unknown>;
  route?: RouteDecision;
}

export interface RouteDecision {
  requested?: string;
  model_class?: string;
  provider?: string;
  model?: string;
  reasons?: string[];
  fallback_used?: boolean;
}

export interface ToolRequest {
  /** namespaced, e.g. fs.read_text */
  tool?: string;
  args?: Record<string, unknown>;
  idempotency_key?: string;
}

export interface ContentBlock {
  /** text | json */
  type?: string;
  text?: string;
  json?: Record<string, unknown>;
}

export interface ToolResult {
  request_id?: string;
  tool?: string;
  is_error?: boolean;
  content?: ContentBlock[];
  structured?: Record<string, unknown>;
  truncated?: boolean;
  /** Tool output is untrusted data and must never be treated as instructions. */
  untrusted?: boolean;
  duration_ms?: number;
}

export interface ToolInfo {
  name?: string;
  server?: string;
  description?: string;
  input_schema?: Record<string, unknown>;
  /** read | write | external */
  side_effect?: string;
}

export interface ListToolsRequest {
}

export interface ListToolsResponse {
  tools?: ToolInfo[];
}

export interface ModelInfo {
  /** alias or class-qualified name */
  name?: string;
  /** local | cloud | enterprise */
  model_class?: string;
  provider?: string;
  model?: string;
  /** chat, stream, tools, json, vision */
  capabilities?: string[];
  available?: boolean;
}

export interface ListModelsRequest {
}

export interface ListModelsResponse {
  models?: ModelInfo[];
}

export interface RegisterAgentRequest {
  /** Agent document (apiVersion uar/v1, kind Agent), validated against
 contracts/schemas/agent.schema.json. */
  definition?: Record<string, unknown>;
}

export interface AgentVersion {
  agent_id?: string;
  version?: string;
  /** sha256 of canonical JSON */
  digest?: string;
  created_at?: string;
  warnings?: string[];
}

export interface RunRequest {
  agent_id?: string;
  /** empty = latest registered */
  version?: string;
  input?: Record<string, unknown>;
  idempotency_key?: string;
}

export interface Run {
  run_id?: string;
  agent_id?: string;
  version?: string;
  /** queued | running | waiting_approval | succeeded | failed | cancelled | needs_attention */
  status?: string;
  output?: Record<string, unknown>;
  error?: Error;
  usage?: Usage;
  steps?: number;
  current_node?: string;
  created_at?: string;
  updated_at?: string;
  parent_run_id?: string;
  cancel_requested?: boolean;
}

export interface GetRunRequest {
  run_id?: string;
}

export interface WatchRunRequest {
  run_id?: string;
  after_seq?: number;
}

export interface CancelRunRequest {
  run_id?: string;
  reason?: string;
}

export interface ResolveRunRequest {
  run_id?: string;
  /** mark_completed | retry_node | fail */
  action?: string;
  note?: string;
}

export interface Started {
  model?: string;
  provider?: string;
  agent_id?: string;
}

export interface TokenDelta {
  text?: string;
}

export interface ToolCallEvent {
  id?: string;
  tool?: string;
  args?: Record<string, unknown>;
  node_id?: string;
}

export interface ToolResultEvent {
  id?: string;
  tool?: string;
  is_error?: boolean;
  summary?: string;
  node_id?: string;
}

export interface NodeStarted {
  node_id?: string;
  node_type?: string;
  attempt?: number;
}

export interface NodeCompleted {
  node_id?: string;
  outcome?: string;
  duration_ms?: number;
  next?: string;
}

export interface ApprovalRequired {
  approval_id?: string;
  action?: string;
  args_hash?: string;
}

export interface Completed {
  status?: string;
  content?: string;
  output?: Record<string, unknown>;
  finish_reason?: string;
}

export interface Event {
  run_id?: string;
  seq?: number;
  ts?: string;
  trace_id?: string;
  request_id?: string;
  started?: Started;
  token?: TokenDelta;
  tool_call?: ToolCallEvent;
  tool_result?: ToolResultEvent;
  node_started?: NodeStarted;
  node_completed?: NodeCompleted;
  usage?: Usage;
  approval_required?: ApprovalRequired;
  /** terminal */
  error?: Error;
  /** terminal */
  completed?: Completed;
  /** Name of the populated body field (JSON/SSE only). */
  type?: "started" | "token" | "tool_call" | "tool_result" | "node_started" | "node_completed" | "usage" | "approval_required" | "error" | "completed";
}

export interface DryRunRequest {
  /** a registered agent (latest or `version`) */
  agent_id?: string;
  /** an unregistered agent document */
  definition?: Record<string, unknown>;
  /** routing preview only */
  inference?: InferenceRequest;
  version?: string;
  input?: Record<string, unknown>;
  /** "static" (default) | "simulate" */
  mode?: string;
  /** simulate: node_id -> fixture output (or list of outputs for repeated visits) */
  fixtures?: Record<string, unknown>;
  seed?: number | string;
}

export interface PlannedStep {
  node_id?: string;
  node_type?: string;
  /** model route, tool name, sub-agent */
  detail?: string;
  permissions?: string[];
  /** planned | simulated | unresolved | denied */
  status?: string;
  /** simulate mode, from fixtures only */
  output?: Record<string, unknown>;
}

export interface DryRunReport {
  request_id?: string;
  mode?: string;
  valid?: boolean;
  errors?: string[];
  warnings?: string[];
  steps?: PlannedStep[];
  routes?: RouteDecision[];
  permissions_required?: string[];
  permissions_missing?: string[];
  unresolved?: string[];
  /** enumerated paths, static mode */
  branches?: string[];
  estimated_input_tokens_max?: number;
  estimated_output_tokens_max?: number;
  /** absent when any price is unknown */
  estimated_cost_max?: Money;
  /** always true; asserted by tests */
  executed_nothing?: boolean;
}

export interface ApprovalDecision {
  approval_id?: string;
  approve?: boolean;
  comment?: string;
}

export interface Approval {
  approval_id?: string;
  status?: string;
  run_id?: string;
}
