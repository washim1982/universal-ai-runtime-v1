/**
 * TypeScript / JavaScript client for the Universal AI Runtime (HTTP/JSON + SSE).
 *
 *   const client = new UAR("http://localhost:9000");            // key from UAR_API_KEY
 *   const resp = await client.inference({ model: "local:default", prompt: "Explain quantum computing" });
 *   console.log(resp.text);
 *
 * Retries: only GET requests and POSTs carrying an idempotency key are retried (429, 503, network
 * errors). Inference and tool calls without a key are never retried automatically.
 */
import type {
  AgentVersion, DryRunReport, Event, InferenceResponse as InferenceResponseMsg, ModelInfo, Run, ToolInfo,
  ToolResult, ChatMessage, Error as ErrorMsg,
} from "./types.gen.js";

export * from "./types.gen.js";
export const VERSION = "0.6.0";

export interface ClientOptions {
  apiKey?: string;
  token?: string;
  timeoutMs?: number;
  maxRetries?: number;
  fetch?: typeof fetch;
}

export interface InferenceParams {
  model?: string;
  prompt?: string;
  messages?: ChatMessage[];
  agent?: string;
  tools?: string[];
  toolMode?: "suggest" | "auto";
  temperature?: number;
  maxTokens?: number;
  topP?: number;
  stop?: string[];
  responseSchema?: Record<string, unknown>;
  dataClass?: string;
  extensions?: Record<string, unknown>;
  signal?: AbortSignal;
}

export type InferenceResponse = InferenceResponseMsg & { readonly text: string };
export type UAREvent = Event & { type: string };

export class UARError extends Error {
  readonly status: number;
  readonly code: string;
  readonly requestId: string;
  readonly retryable: boolean;
  readonly details: Record<string, unknown>;
  constructor(status: number, err: ErrorMsg) {
    super(`${err.code ?? "error"}: ${err.message ?? ""} (status ${status}, request ${err.request_id ?? ""})`);
    this.name = new.target.name;
    this.status = status;
    this.code = err.code ?? "unknown";
    this.requestId = err.request_id ?? "";
    this.retryable = Boolean(err.retryable);
    this.details = (err.details as Record<string, unknown>) ?? {};
  }
}
export class InvalidRequestError extends UARError {}
export class AuthenticationError extends UARError {}
export class PermissionDeniedError extends UARError {}
export class NotFoundError extends UARError {}
export class ConflictError extends UARError {}
export class RateLimitError extends UARError {}
export class ServerError extends UARError {}
export class UnavailableError extends ServerError {}

function toError(status: number, payload: unknown): UARError {
  const err = ((payload as { error?: ErrorMsg })?.error ?? { code: "http_error", message: String(payload) }) as ErrorMsg;
  const cls = ({ 400: InvalidRequestError, 401: AuthenticationError, 403: PermissionDeniedError, 404: NotFoundError,
    409: ConflictError, 413: InvalidRequestError, 422: InvalidRequestError, 429: RateLimitError,
    503: UnavailableError } as Record<number, typeof UARError>)[status] ?? (status >= 500 ? ServerError : UARError);
  return new cls(status, err);
}

function inferenceBody(p: InferenceParams, stream: boolean): Record<string, unknown> {
  const body: Record<string, unknown> = {};
  if (p.model) body.model = p.model;
  if (p.messages?.length) body.messages = p.messages;
  else if (p.prompt !== undefined) body.input = p.prompt;
  if (p.agent) body.agent = p.agent;
  if (p.tools?.length) body.tools = p.tools;
  if (p.toolMode) body.tool_mode = p.toolMode;
  if (p.dataClass) body.data_class = p.dataClass;
  if (p.extensions) body.extensions = p.extensions;
  const params: Record<string, unknown> = {};
  if (p.temperature !== undefined) params.temperature = p.temperature;
  if (p.maxTokens !== undefined) params.max_tokens = p.maxTokens;
  if (p.topP !== undefined) params.top_p = p.topP;
  if (p.stop) params.stop = p.stop;
  if (p.responseSchema) params.response_schema = p.responseSchema;
  if (Object.keys(params).length) body.params = params;
  if (stream) body.stream = true;
  return body;
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
const env = (name: string): string | undefined =>
  (globalThis as { process?: { env?: Record<string, string | undefined> } }).process?.env?.[name];

export class UAR {
  readonly baseUrl: string;
  private readonly apiKey?: string;
  private readonly token?: string;
  private readonly timeoutMs: number;
  private readonly maxRetries: number;
  private readonly fetchImpl: typeof fetch;

  constructor(baseUrl = "http://localhost:9000", opts: ClientOptions = {}) {
    this.baseUrl = baseUrl.replace(/\/+$/, "");
    this.apiKey = opts.apiKey ?? env("UAR_API_KEY");
    this.token = opts.token;
    this.timeoutMs = opts.timeoutMs ?? 120_000;
    this.maxRetries = opts.maxRetries ?? 2;
    this.fetchImpl = opts.fetch ?? fetch;
  }

  private headers(idempotencyKey?: string, accept = "application/json"): Record<string, string> {
    const h: Record<string, string> = { "Content-Type": "application/json", Accept: accept };
    if (this.apiKey) h["X-API-Key"] = this.apiKey;
    else if (this.token) h.Authorization = `Bearer ${this.token}`;
    if (idempotencyKey) h["Idempotency-Key"] = idempotencyKey;
    return h;
  }

  private async request<T>(method: string, path: string, body?: unknown, idempotencyKey?: string,
                           signal?: AbortSignal): Promise<T> {
    const retryable = method === "GET" || Boolean(idempotencyKey);
    for (let attempt = 0; ; attempt++) {
      let res: Response | undefined;
      try {
        res = await this.fetchImpl(this.baseUrl + path, {
          method, headers: this.headers(idempotencyKey), body: body === undefined ? undefined : JSON.stringify(body),
          signal: signal ?? AbortSignal.timeout(this.timeoutMs),
        });
        const payload = await res.json().catch(() => ({}));
        if (res.ok) return payload as T;
        if (!(res.status === 429 || res.status === 503) || !retryable || attempt >= this.maxRetries) {
          throw toError(res.status, payload);
        }
      } catch (e) {
        if (e instanceof UARError) throw e;
        if (!retryable || attempt >= this.maxRetries) throw e;
      }
      const ra = Number(res?.headers.get("retry-after"));
      await sleep(Number.isFinite(ra) && ra > 0 ? Math.min(ra, 30) * 1000
        : Math.min(250 * 2 ** attempt, 5000) * (0.5 + Math.random()));
    }
  }

  private async *sse(method: string, path: string, body?: unknown, signal?: AbortSignal): AsyncGenerator<UAREvent> {
    const res = await this.fetchImpl(this.baseUrl + path, {
      method, headers: this.headers(undefined, "text/event-stream"),
      body: body === undefined ? undefined : JSON.stringify(body), signal,
    });
    if (!res.ok || !res.body) throw toError(res.status, await res.json().catch(() => ({})));
    const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
    let buf = "";
    let data: string[] = [];
    try {
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += value;
        let nl: number;
        while ((nl = buf.indexOf("\n")) !== -1) {
          const line = buf.slice(0, nl).replace(/\r$/, "");
          buf = buf.slice(nl + 1);
          if (line === "") {
            if (data.length) yield JSON.parse(data.join("\n")) as UAREvent;
            data = [];
          } else if (line.startsWith("data:")) {
            data.push(line.slice(5).trimStart());
          }
        }
      }
      if (data.length) yield JSON.parse(data.join("\n")) as UAREvent;
    } finally {
      // Breaking out of the loop cancels the HTTP stream; the runtime stops the provider request.
      await reader.cancel().catch(() => undefined);
    }
  }

  // ---------------------------------------------------------------- inference

  /** Synchronous inference. With `agent`, runs that agent on the prompt and returns its result. */
  async inference(p: InferenceParams): Promise<InferenceResponse> {
    const r = await this.request<InferenceResponseMsg>("POST", "/api/v1/inference", inferenceBody(p, false),
      undefined, p.signal);
    return Object.defineProperty(r, "text", { get: () => r.content ?? "", enumerable: false }) as InferenceResponse;
  }

  /** Streaming inference: started, token..., usage, then exactly one completed | error event. */
  stream(p: InferenceParams): AsyncGenerator<UAREvent> {
    return this.sse("POST", "/api/v1/inference", inferenceBody(p, true), p.signal);
  }

  // ---------------------------------------------------------------- tools & catalog

  executeTool(tool: string, args: Record<string, unknown> = {}, opts: { idempotencyKey?: string } = {}) {
    return this.request<ToolResult>("POST", "/api/v1/tool/execute", { tool, args }, opts.idempotencyKey);
  }

  async listModels(): Promise<ModelInfo[]> {
    return (await this.request<{ models?: ModelInfo[] }>("GET", "/api/v1/models")).models ?? [];
  }

  async listTools(): Promise<ToolInfo[]> {
    return (await this.request<{ tools?: ToolInfo[] }>("GET", "/api/v1/tools")).tools ?? [];
  }

  // ---------------------------------------------------------------- agents & runs

  registerAgent(definition: Record<string, unknown>) {
    return this.request<AgentVersion>("POST", "/api/v1/agents", { definition });
  }

  async runAgent(agentId: string, input: Record<string, unknown> = {},
                 opts: { version?: string; idempotencyKey?: string; wait?: boolean; timeoutMs?: number } = {}) {
    const body: Record<string, unknown> = { agent_id: agentId, input };
    if (opts.version) body.version = opts.version;
    const run = await this.request<Run>("POST", "/api/v1/agent/run", body, opts.idempotencyKey);
    return opts.wait ? this.waitRun(run.run_id!, opts.timeoutMs) : run;
  }

  getRun(runId: string) {
    return this.request<Run>("GET", `/api/v1/runs/${encodeURIComponent(runId)}`);
  }

  async waitRun(runId: string, timeoutMs = 600_000): Promise<Run> {
    const deadline = Date.now() + timeoutMs;
    for (;;) {
      const run = await this.getRun(runId);
      if (["succeeded", "failed", "cancelled", "needs_attention"].includes(run.status ?? "")) return run;
      if (Date.now() > deadline) throw new Error(`run ${runId} still ${run.status}`);
      await sleep(250);
    }
  }

  watchRun(runId: string, afterSeq = 0, signal?: AbortSignal): AsyncGenerator<UAREvent> {
    return this.sse("GET", `/api/v1/runs/${encodeURIComponent(runId)}/events?after_seq=${afterSeq}`, undefined, signal);
  }

  cancelRun(runId: string, reason = "") {
    return this.request<Run>("POST", `/api/v1/runs/${encodeURIComponent(runId)}/cancel`, reason ? { reason } : {});
  }

  resolveRun(runId: string, action: "mark_completed" | "retry_node" | "fail", note = "") {
    return this.request<Run>("POST", `/api/v1/runs/${encodeURIComponent(runId)}/resolve`, { action, note });
  }

  dryRun(req: { agentId?: string; definition?: Record<string, unknown>; inference?: Record<string, unknown>;
                mode?: "static" | "simulate"; input?: Record<string, unknown>; fixtures?: Record<string, unknown>;
                version?: string }) {
    const body: Record<string, unknown> = { mode: req.mode ?? "static" };
    if (req.agentId) body.agent_id = req.agentId;
    else if (req.definition) body.definition = req.definition;
    else if (req.inference) body.inference = req.inference;
    if (req.input) body.input = req.input;
    if (req.fixtures) body.fixtures = req.fixtures;
    if (req.version) body.version = req.version;
    return this.request<DryRunReport>("POST", "/api/v1/dry-run", body);
  }
}

export default UAR;
