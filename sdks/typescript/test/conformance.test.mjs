// TypeScript SDK conformance: the shared fixtures through the SDK, against uar-mock (default) or a
// live runtime (UAR_LIVE_URL + UAR_LIVE_KEY, test configuration).
import { test, before, after } from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { readFileSync, readdirSync, existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import net from "node:net";
import { UAR, AuthenticationError, PermissionDeniedError, NotFoundError, UnavailableError } from "../dist/index.js";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "..");
const FIX = Object.fromEntries(readdirSync(join(ROOT, "contracts", "fixtures")).filter((f) => f.endsWith(".json"))
  .map((f) => [f.replace(/\.json$/, ""), JSON.parse(readFileSync(join(ROOT, "contracts", "fixtures", f), "utf8"))]));

let url, key, mock;

const freePort = () => new Promise((res) => { const s = net.createServer(); s.listen(0, () => { const p = s.address().port; s.close(() => res(p)); }); });

before(async () => {
  if (process.env.UAR_LIVE_URL) { url = process.env.UAR_LIVE_URL; key = process.env.UAR_LIVE_KEY; return; }
  const port = await freePort();
  const venv = join(ROOT, ".venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
  const py = process.env.UAR_PYTHON ?? (existsSync(venv) ? venv : "python");
  mock = spawn(py, [join(ROOT, "mock", "uar_mock.py"), "--port", String(port)], { stdio: "ignore" });
  url = `http://127.0.0.1:${port}`;
  key = "uar_mock0000_notasecretjustamockkey";
  for (let i = 0; i < 100; i++) {
    try { await fetch(url + "/"); return; } catch { await new Promise((r) => setTimeout(r, 100)); }
  }
  throw new Error("uar-mock did not start");
});
after(() => mock?.kill());

function strip(value, ignore) {
  const v = structuredClone(value);
  for (const path of ignore) {
    const parts = path.split(".");
    let cur = v;
    for (const p of parts.slice(0, -1)) cur = cur && typeof cur === "object" ? (cur[p] ?? {}) : {};
    if (cur && typeof cur === "object") delete cur[parts.at(-1)];
  }
  return v;
}
const expectFixture = (name, got) => {
  const r = FIX[name].response;
  assert.deepEqual(strip(JSON.parse(JSON.stringify(got)), r.ignore), strip(r.body, r.ignore));
};

const tests = {
  inference_basic: async () => {
    const resp = await new UAR(url, { apiKey: key }).inference({ model: "local:default", prompt: "hello" });
    expectFixture("inference_basic", resp);
    assert.equal(resp.text, "echo: hello");
  },
  inference_stream: async () => {
    const events = [];
    for await (const ev of new UAR(url, { apiKey: key }).stream({ model: "local:default", prompt: "stream" })) events.push(ev);
    const r = FIX.inference_stream.response;
    assert.deepEqual(events.map(({ type, ...e }) => strip(e, r.ignore)), r.events.map((e) => strip(e, r.ignore)));
    assert.equal(events.at(-1).type, "completed");
  },
  error_unauthenticated: async () => {
    await assert.rejects(new UAR(url, { apiKey: "" }).inference({ model: "local:default", prompt: "hi" }),
      (e) => e instanceof AuthenticationError && e.code === "unauthenticated" && e.status === 401);
  },
  tool_execute_read: async () => {
    const res = await new UAR(url, { apiKey: key }).executeTool("fs.read_text", { path: "docs/faq.md" });
    expectFixture("tool_execute_read", res);
  },
  tool_policy_denied: async () => {
    await assert.rejects(new UAR(url, { apiKey: key }).executeTool("fs.write_text", { path: "docs/x.md", content: "x" }),
      (e) => e instanceof PermissionDeniedError && e.code === "policy_denied");
  },
  run_not_found: async () => {
    await assert.rejects(new UAR(url, { apiKey: key }).getRun("run_does_not_exist"),
      (e) => e instanceof NotFoundError && e.code === "not_found");
  },
  run_start: async () => {
    const run = await new UAR(url, { apiKey: key }).runAgent("in_app_assistant", { prompt: "What is the return window?" });
    expectFixture("run_start", run);
  },
  dry_run_static: async () => {
    const rep = await new UAR(url, { apiKey: key }).dryRun({ agentId: "in_app_assistant", mode: "static" });
    expectFixture("dry_run_static", rep);
  },
};

for (const name of Object.keys(FIX)) {
  test(`fixture ${name}`, async () => {
    assert.ok(tests[name], `no SDK test for fixture ${name}`);
    await tests[name]();
  });
}

test("inference is not retried without an idempotency key", async () => {
  let calls = 0;
  const fake = async () => { calls++; return new Response(JSON.stringify({ error: { code: "unavailable" } }), { status: 503 }); };
  const c = new UAR("http://unused", { apiKey: "k", maxRetries: 3, fetch: fake });
  await assert.rejects(c.inference({ model: "m", prompt: "p" }), UnavailableError);
  assert.equal(calls, 1);
});
