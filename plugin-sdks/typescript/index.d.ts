export interface Reply {
  text?: string;
  tokens?: string[];
  output?: Record<string, unknown>;
  finishReason?: string;
  inputTokens?: number;
  outputTokens?: number;
}

export interface ModelCall {
  model: string;
  messages: Array<Record<string, unknown>>;
  params: Record<string, unknown>;
  tools: Array<Record<string, unknown>>;
}

export interface ToolDef {
  name: string;
  description?: string;
  inputSchema?: Record<string, unknown>;
  /** Advisory only: the administrator's manifest decides the side-effect class. */
  sideEffect?: "read" | "write" | "external";
  run(args: Record<string, unknown>, ctx: unknown): Reply | Promise<Reply>;
}

export interface PluginDef {
  id: string;
  version: string;
  kind: "model" | "tool" | "agent";
  models?: string[];
  capabilities?: string[];
  tools?: ToolDef[];
  init?(config: Record<string, unknown>, secrets: Record<string, string>): void | Promise<void>;
  model?(call: ModelCall, ctx: unknown): Reply | Promise<Reply>;
  agent?(agent: string, input: Record<string, unknown>, ctx: unknown): Reply | Promise<Reply>;
}

export class PluginError extends Error {
  code: string;
  constructor(message: string, code?: string);
}

export function serve(plugin: PluginDef): void;
export function toStruct(obj: Record<string, unknown>): unknown;
export function fromStruct(s: unknown): Record<string, unknown>;
