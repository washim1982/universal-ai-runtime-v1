// Write UAR plugins in JavaScript/TypeScript.
//
//   import { serve } from "@uar/plugin-sdk";
//   serve({
//     id: "acme.textutil", version: "1.0.0", kind: "tool",
//     tools: [{ name: "word_count", description: "...", inputSchema: {...},
//               run: async (args) => ({ output: { words: 3 } }) }],
//   });
//
// The runtime starts the plugin with UAR_PLUGIN_ADDR set and speaks uar.plugin.v1 over gRPC.
// Handlers return { text?, output?, tokens?, finishReason?, inputTokens?, outputTokens? } or throw
// PluginError for an error the caller should see.
import grpc from "@grpc/grpc-js";
import protoLoader from "@grpc/proto-loader";
import { existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const PROTO_ROOTS = [join(here, "proto"), join(here, "..", "..", "proto")];

export class PluginError extends Error {
  constructor(message, code = "plugin_error") { super(message); this.code = code; }
}

// google.protobuf.Struct <-> plain JSON
function toValue(v) {
  if (v === null || v === undefined) return { nullValue: "NULL_VALUE" };
  if (typeof v === "number") return { numberValue: v };
  if (typeof v === "string") return { stringValue: v };
  if (typeof v === "boolean") return { boolValue: v };
  if (Array.isArray(v)) return { listValue: { values: v.map(toValue) } };
  return { structValue: toStruct(v) };
}
function fromValue(v) {
  if (!v) return null;
  if ("stringValue" in v) return v.stringValue;
  if ("numberValue" in v) return v.numberValue;
  if ("boolValue" in v) return v.boolValue;
  if ("structValue" in v) return fromStruct(v.structValue);
  if ("listValue" in v) return (v.listValue.values || []).map(fromValue);
  return null;
}
export function toStruct(obj) {
  return { fields: Object.fromEntries(Object.entries(obj || {}).map(([k, v]) => [k, toValue(v)])) };
}
export function fromStruct(s) {
  return Object.fromEntries(Object.entries((s && s.fields) || {}).map(([k, v]) => [k, fromValue(v)]));
}

function loadService() {
  const root = PROTO_ROOTS.find((r) => existsSync(join(r, "uarpb/plugin/v1/plugin.proto")));
  if (!root) throw new Error("plugin.proto not found (expected next to the SDK or in the repository's proto/)");
  const def = protoLoader.loadSync(join(root, "uarpb/plugin/v1/plugin.proto"), {
    keepCase: true, longs: Number, enums: String, defaults: true, oneofs: true, includeDirs: [root],
  });
  return grpc.loadPackageDefinition(def).uar.plugin.v1.Plugin;
}

function finalResponse(r) {
  const text = r.text ?? (r.tokens ? r.tokens.join("") : "");
  return { text, output: r.output ? toStruct(r.output) : undefined, finish_reason: r.finishReason || "stop",
           input_tokens: r.inputTokens || 0, output_tokens: r.outputTokens || 0 };
}
function errorResponse(e) {
  return e instanceof PluginError ? { is_error: true, error_code: e.code, error_message: e.message }
    : { is_error: true, error_code: "internal", error_message: String(e && e.message || e).slice(0, 500) };
}

export function serve(plugin) {
  const Service = loadService();
  const server = new grpc.Server();
  const dispatch = async (req) => {
    if (req.call === "model") {
      if (!plugin.model) throw new PluginError("this plugin does not serve models", "unimplemented");
      const m = req.model;
      return plugin.model({ model: m.model, messages: m.messages.map(fromStruct), params: fromStruct(m.params),
                            tools: m.tools.map(fromStruct) }, req.ctx);
    }
    if (req.call === "tool") {
      const t = (plugin.tools || []).find((x) => x.name === req.tool.tool);
      if (!t) throw new PluginError(`unknown tool ${req.tool.tool}`, "not_found");
      return t.run(fromStruct(req.tool.args), req.ctx);
    }
    if (req.call === "agent") {
      if (!plugin.agent) throw new PluginError("this plugin does not run agents", "unimplemented");
      return plugin.agent(req.agent.agent, fromStruct(req.agent.input), req.ctx);
    }
    throw new PluginError("empty request", "invalid_argument");
  };
  server.addService(Service.service, {
    Describe: (_call, cb) => cb(null, {
      id: plugin.id, version: plugin.version, kind: plugin.kind, api: "uar.plugin.v1",
      capabilities: plugin.capabilities || [], models: plugin.models || [],
      tools: (plugin.tools || []).map((t) => ({ name: t.name, description: t.description || "",
        input_schema: toStruct(t.inputSchema || { type: "object" }), side_effect: t.sideEffect || "read" })),
    }),
    Init: async (call, cb) => {
      try { await plugin.init?.(fromStruct(call.request.config), call.request.secrets || {}); cb(null, { ok: true }); }
      catch (e) { cb(null, { ok: false, message: String(e.message || e) }); }
    },
    Execute: async (call, cb) => {
      try { cb(null, finalResponse(await dispatch(call.request))); } catch (e) { cb(null, errorResponse(e)); }
    },
    ExecuteStream: async (call) => {
      try {
        const r = await dispatch(call.request);
        for (const t of r.tokens || (r.text ? [r.text] : [])) call.write({ token: t });
        call.write({ final: finalResponse(r) });
      } catch (e) { call.write({ final: errorResponse(e) }); }
      call.end();
    },
    Health: (_call, cb) => cb(null, { serving: true, message: "ok" }),
    Shutdown: (_call, cb) => { cb(null, {}); setTimeout(() => server.tryShutdown(() => process.exit(0)), 100); },
  });
  const addr = process.env.UAR_PLUGIN_ADDR || "127.0.0.1:50061";
  server.bindAsync(addr, grpc.ServerCredentials.createInsecure(), (err) => {
    if (err) { console.error(err); process.exit(1); }
    console.error(`${plugin.id}@${plugin.version} listening on ${addr}`);
  });
  for (const s of ["SIGINT", "SIGTERM"]) process.on(s, () => server.tryShutdown(() => process.exit(0)));
}
