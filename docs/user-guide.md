# UAR user guide: setup and complete testing

This guide takes you from a fresh checkout to a fully tested Universal AI Runtime. It then shows how
to re-test after you change something. Every command here was run on the reference machine
(Windows 11, PowerShell 7) on 2026-09-27.

> **Prefer buttons to commands?** Double-click `setup.cmd` in the project folder. It opens a setup
> dashboard with a Run button for every setup and test step below, and a live view of which services
> are up. The beginner walkthrough for a brand-new PC is [user-guide.html](user-guide.html). This
> document is the command-line reference and the detailed feature test plan.

Commands are for **PowerShell** and run from the repository root:
`C:\Users\wasim\Claude\Projects\Universal-AI-Runtime-v1`.

| Part | What you do | Time |
|---|---|---|
| [1. Prerequisites](#1-prerequisites) | Check the tools are installed | 5 min |
| [2. One-time setup](#2-one-time-setup) | Python env, fixtures, keys, database | 10 min |
| [3. Start the runtime](#3-start-the-runtime) | Local process or Docker Compose | 2 min |
| [4. One-command end-to-end check](#4-one-command-end-to-end-check) | `scripts/smoke_test.py` (27 checks) | 1–2 min |
| [5. Manual walkthrough](#5-manual-walkthrough-feature-by-feature) | Try each feature yourself | 30 min |
| [6. Automated test suites](#6-automated-test-suites) | Unit, contract, security, recovery, live, SDK | 5–10 min |
| [7. Testing a change](#7-testing-a-change) | What to run for each kind of change | — |
| [8. Kubernetes](#8-kubernetes-docker-desktop) | Helm install on Docker Desktop (not yet verified) | 20 min |
| [9. Troubleshooting](#9-troubleshooting) and [10. Reset](#10-stop-reset-and-clean-up) | | |

---

## 1. Prerequisites

| Tool | Version used | Check |
|---|---|---|
| Python | 3.12+ (3.14.6 tested) | `python --version` |
| Docker Desktop | 29.x, running | `docker info --format '{{.ServerVersion}}'` |
| Node.js | 18+ (24 tested), only for the TypeScript SDK | `node --version` |
| Ollama | any recent, with the model pulled | `ollama list` shows `granite4:latest` |
| Optional: LM Studio, llama.cpp `llama-server` | for extra live tests | ports 1234 / 8080 |
| Optional: Helm 3, Kubernetes in Docker Desktop | for part 8 | `helm version`, `kubectl get nodes` |

If `granite4:latest` is missing, run `ollama pull granite4:latest`. You can use any other chat
model instead: after setup, change `local:default` in `.local\uar.yaml`.

---

## 2. One-time setup

**Step 1: create the Python environment and install the runtime and the Python SDK.**

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev]" -e sdks\python
```

**Step 2: create the demo fixtures.** This writes a SQLite sales database and a small workspace
containing a product FAQ.

```powershell
.\.venv\Scripts\python.exe scripts\make_fixtures.py
```

Expected output: `fixtures written: examples\fixtures\sales.db, examples\workspace`

**Step 3: generate your private configuration and API keys.**

```powershell
.\.venv\Scripts\python.exe scripts\bootstrap_local.py
```

This writes `.local\uar.yaml`, which holds hashes of your keys, and `.local\credentials.env`, which
holds the keys themselves. Both files are git-ignored. You get three keys:

| Variable | Role | Can do |
|---|---|---|
| `UAR_DEV_KEY` | developer | inference (local models), tools, agents, runs, dry-run |
| `UAR_OPS_KEY` | operator | read, cancel, resolve runs, dry-run. No file writes, no inference |
| `UAR_ADMIN_KEY` | admin | everything |

Existing keys are reused when you re-run the script; add `--force` for new ones.

**Step 4: start PostgreSQL.**

```powershell
docker compose -f deploy/docker/compose.yaml up -d postgres
docker compose -f deploy/docker/compose.yaml ps
```

Expected: `uar-postgres-1 ... (healthy)`, listening on `127.0.0.1:55440`.

**Step 5: validate the configuration.**

```powershell
.\.venv\Scripts\uar.exe check-config --config .local\uar.yaml
```

Expected: `ok: profile=standard providers=6 mcp_servers=2`

**Step 6 (optional): build the TypeScript SDK.**

```powershell
cd sdks\typescript; npm install; npm run build; cd ..\..
```

**Step 7: load your keys into the current shell.** You need to do this in every new PowerShell
window.

```powershell
$creds = Get-Content .local\credentials.env
$env:UAR_API_KEY = (($creds | Select-String '^UAR_DEV_KEY=').Line -split '=',2)[1]
$OPS = (($creds | Select-String '^UAR_OPS_KEY=').Line -split '=',2)[1]
$H = @{ 'X-API-Key' = $env:UAR_API_KEY }
$BASE = 'http://localhost:9000'
```

---

## 3. Start the runtime

Choose **A** while you develop (fast restarts) or **B** to test the container that you ship. Both
serve HTTP on port 9000 and gRPC on port 50051. Don't run both at once, because they use the same
ports.

### A. Local process

```powershell
.\.venv\Scripts\uar.exe serve --config .local\uar.yaml
```

Leave it running and use a second window. The startup log line `runtime started` lists what was
found:

```text
"migrations": [...], "providers": {"ollama": "8 models", "lmstudio": "3 models", ...},
"mcp": {"fs": "3 tools", "db": "2 tools"}, "agents": ["in_app_assistant", "report_generator"]
```

Stop it with `Ctrl+C`.

### B. Docker Compose (runtime image + Postgres + Jaeger)

```powershell
docker build -f deploy/docker/Dockerfile -t uar-runtime:0.9.0 .
docker compose -f deploy/docker/compose.yaml --profile full up -d
docker logs uar-uar-1 2>&1 | Select-String "runtime started"
```

The container reaches your host's Ollama, LM Studio and llama.cpp through `host.docker.internal`.
Traces go to Jaeger at http://localhost:16686. Reports are written to
`examples\workspace\reports`, which is bind-mounted into the container.

**Check that it is ready** (either option):

```powershell
Invoke-RestMethod "$BASE/readyz" | ConvertTo-Json -Compress
```

Expected: `{"ready":true,"database":true,"mcp":{"fs":true,"db":true}}`

---

### C. From UAR Admin (Windows app)

Double-click `admin.cmd` in the project folder. The first run builds the app (needs the .NET SDK 8
or newer); after that it opens directly. It connects to `http://127.0.0.1:9000` with the local
admin key from `.local\credentials.env`.

| Page | Use it to |
|---|---|
| Overview | Start, stop or restart the runtime; see the health of the database, model providers, tool servers, plugins and worker |
| Access & keys | Create API keys with chosen roles and expiry (shown once), revoke keys, review roles and SSO mappings |
| API reference | Browse every endpoint, try requests with your key, copy `curl` commands |
| Inference history | Model calls per user and model, with tokens and cost; CSV export |
| Approvals | Approve or reject agent actions that wait for a human |
| Audit trail | Read and export the audit log; **Verify chain** detects tampering |
| Service logs | Live runtime logs, or the output of a runtime the app started |

The admin pages need runtime 0.9 or newer. If you use the Docker runtime (option B), rebuild the
image first. Details: [admin-app/README.md](../admin-app/README.md).

---

## 4. One-command end-to-end check

Run this first after any change, against whichever deployment you started:

```powershell
.\.venv\Scripts\python.exe scripts\smoke_test.py
```

It reads the keys from `.local\credentials.env`, uses real models, and checks 27 things. The
expected ending is:

```text
[1] health            readyz
[2] authentication    401 without / with a bad key, operator write 403, cloud without permission 403, unknown field 400
[3] inference         HTTP sync, SSE stream, gRPC, WebSocket, model catalog
[4] MCP tools         catalog, read-only SQL, SQL write rejected, traversal rejected, policy denial,
                      authorized write, overwrite refused
[5] agents            idempotent start, report agent succeeds, ordered events, auth on runs,
                      input schema, inference with agent=
[6] dry-run           static plan, fixture simulation
[7] observability     Prometheus metrics
27/27 checks passed
```

The exit code is 0 only if everything passed. Other targets:
`--http http://127.0.0.1:9000 --grpc 127.0.0.1:50051 --model local:ollama/granite4:latest --dev-key ... --ops-key ...`

---

## 5. Manual walkthrough, feature by feature

Every step shows the command and what you should see. Run step 7 of setup first.

### 5.1 Inference (synchronous)

```powershell
$r = Invoke-RestMethod -Method Post "$BASE/api/v1/inference" -Headers $H -ContentType 'application/json' `
  -Body '{"model":"local:default","input":"Say hi in 3 words","params":{"max_tokens":20}}'
"$($r.provider)/$($r.model): $($r.content)"
$r.route | ConvertTo-Json -Compress
$r.usage | ConvertTo-Json -Compress
```

Expected: `ollama/granite4:latest: Hello there!` (the wording varies). `route.reasons` explains the
routing decision, for example `alias local:default -> ollama/granite4:latest` and `policy allowed`.
`usage` shows the token counts and `cost {"amount":"0"}`, because local models are priced at zero.

**Model naming:** try `local:ollama/granite4:latest` (explicit provider),
`local:lmstudio/qwen/qwen3.8-27b`, or a bare `granite4:latest`. A bare name uses the default class,
`local`.

### 5.2 Streaming (SSE)

```powershell
curl.exe -s -N -X POST "$BASE/api/v1/inference" -H "X-API-Key: $env:UAR_API_KEY" -H 'Content-Type: application/json' `
  -d '{"model":"local:default","input":"Count 1 to 3","stream":true,"params":{"max_tokens":20}}'
```

Expected: `event: started`, then many `event: token`, one `event: usage`, and exactly one terminal
`event: completed`. Every event has an increasing `seq`.

### 5.3 Structured JSON output

```powershell
$body = @{ model='local:default'; input='What is the capital of France?'
  params=@{ max_tokens=80; temperature=0; response_schema=@{ type='object'; required=@('city','country')
  properties=@{ city=@{type='string'}; country=@{type='string'} } } } } | ConvertTo-Json -Depth 8
(Invoke-RestMethod -Method Post "$BASE/api/v1/inference" -Headers $H -ContentType 'application/json' -Body $body).content
```

Expected: `{"city": "Paris", "country": "France"}`

### 5.4 gRPC

```powershell
@'
import os, grpc
from uarpb.v1 import runtime_pb2 as pb, runtime_pb2_grpc as pbg
with grpc.insecure_channel("localhost:50051") as ch:
    r = pbg.RuntimeStub(ch).Infer(pb.InferenceRequest(model="local:default", input="Name a planet"),
                                  metadata=[("x-api-key", os.environ["UAR_API_KEY"])])
    print(r.provider, r.model, "->", r.content)
'@ | .\.venv\Scripts\python.exe -
```

Expected: `ollama granite4:latest -> ...`. Errors come back as gRPC status codes, with the full UAR
error as JSON in the `uar-error` trailing metadata.

### 5.5 WebSocket

```powershell
@'
import asyncio, json, os, websockets
async def main():
    async with websockets.connect("ws://localhost:9000/api/v1/ws",
                                  additional_headers={"X-API-Key": os.environ["UAR_API_KEY"]}) as ws:
        await ws.send(json.dumps({"id": "1", "op": "infer",
                                  "body": {"model": "local:default", "input": "Name 3 colors", "params": {"max_tokens": 30}}}))
        while True:
            f = json.loads(await ws.recv())
            if f.get("event", {}).get("type") == "token":
                print(f["event"]["token"]["text"], end="", flush=True)
            if f.get("done") or f.get("error"):
                print("\n", f.get("error") or "done"); break
asyncio.run(main())
'@ | .\.venv\Scripts\python.exe -
```

Expected: the tokens stream in, then `done`. Other WebSocket operations are `start_run`,
`watch_run`, `get_run`, `cancel_run`, `list_models`, `list_tools`, `execute_tool` and `dry_run`.
`{"op":"cancel","body":{"target":"1"}}` stops an operation that is still running.

### 5.6 Authentication and permissions

```powershell
# no key -> 401
try { Invoke-RestMethod "$BASE/api/v1/models" } catch { $_.Exception.Response.StatusCode.value__ }
# cloud model without the inference:cloud permission -> 403 permission_denied
try { Invoke-RestMethod -Method Post "$BASE/api/v1/inference" -Headers $H -ContentType 'application/json' -Body '{"model":"cloud:default","input":"x"}' } catch { $_.ErrorDetails.Message }
# operator may not write files -> 403
try { Invoke-RestMethod -Method Post "$BASE/api/v1/tool/execute" -Headers @{'X-API-Key'=$OPS} -ContentType 'application/json' -Body '{"tool":"fs.write_text","args":{"path":"reports/ops.md","content":"x"}}' } catch { $_.Exception.Response.StatusCode.value__ }
```

Expected: `401`, then a `permission_denied` error body, then `403`. Every error has the same
shape: `{"error":{"code","message","request_id","retryable","details"}}`. The error codes are
listed in [contracts/errors.md](../contracts/errors.md).

### 5.7 MCP tools (governed)

```powershell
(Invoke-RestMethod "$BASE/api/v1/tools" -Headers $H).tools | Select-Object name, side_effect
function Tool($json) { try { Invoke-RestMethod -Method Post "$BASE/api/v1/tool/execute" -Headers $H -ContentType 'application/json' -Body $json } catch { $_.ErrorDetails.Message } }
Tool '{"tool":"fs.read_text","args":{"path":"docs/product-faq.md"}}'              # is_error False, structured.content = FAQ
Tool '{"tool":"db.query","args":{"sql":"SELECT region, SUM(units) AS u FROM sales GROUP BY region"}}'
Tool '{"tool":"db.query","args":{"sql":"DELETE FROM sales"}}'                    # is_error True (read-only)
Tool '{"tool":"fs.read_text","args":{"path":"../outside.txt"}}'                  # is_error True (traversal)
Tool '{"tool":"fs.write_text","args":{"path":"docs/x.md","content":"x"}}'        # 403 policy_denied
Tool '{"tool":"fs.write_text","args":{"path":"reports/manual.md","content":"hi"}}' # created
Tool '{"tool":"fs.write_text","args":{"path":"reports/manual.md","content":"again"}}' # is_error True (never overwrites)
```

Tool output is always marked `untrusted: true`. Each call writes an audit intent row before the
call and a completion row after it. Write tools also get a durable intent record, which is what
makes crash recovery safe (5.9).

**Automatic tool use by the model** (the gateway runs a bounded tool loop):

```powershell
$body = @{ model='local:default'; tool_mode='auto'; tools=@('fs.read_text'); params=@{temperature=0; max_tokens=200}
  messages=@(@{role='system';content='Use tools to answer. Files are relative paths.'},
             @{role='user';content='Read docs/product-faq.md and tell me how many days customers have to return items.'}) } | ConvertTo-Json -Depth 6
(Invoke-RestMethod -Method Post "$BASE/api/v1/inference" -Headers $H -ContentType 'application/json' -Body $body).content
```

Expected: an answer that mentions 30 days. With `tool_mode='suggest'`, you get `tool_calls` back
and the tool is not executed.

### 5.8 Agents

**Run the report generator** (database query → local model → Markdown report file):

```powershell
$run = Invoke-RestMethod -Method Post "$BASE/api/v1/agent/run" -Headers $H -ContentType 'application/json' `
  -Body '{"agent_id":"report_generator","input":{"report_name":"manual_q2","quarter":"2026-Q2"}}'
do { Start-Sleep 1; $s = Invoke-RestMethod "$BASE/api/v1/runs/$($run.run_id)" -Headers $H } while ($s.status -in 'queued','running')
$s.status; $s.output | ConvertTo-Json -Compress; $s.usage | ConvertTo-Json -Compress
Get-Content examples\workspace\reports\manual_q2.md
```

Expected: `succeeded`, then `{"path":"reports/manual_q2.md","title":"..."}` and the report text.
The small model sometimes gets the arithmetic wrong. That is a model limitation, not a runtime
error.

**Replay the run's events** (resumable: add `-H "Last-Event-ID: 5"` or `?after_seq=5`):

```powershell
curl.exe -s -N "$BASE/api/v1/runs/$($run.run_id)/events" -H "X-API-Key: $env:UAR_API_KEY" | Select-String '^event:'
```

Expected: `started`, then `node_started`, `tool_call`, `tool_result` and `node_completed` for each
of sales, analyse, render, write and done, then `completed`.

**Agent through inference** (the original SDK shorthand):

```powershell
(Invoke-RestMethod -Method Post "$BASE/api/v1/inference" -Headers $H -ContentType 'application/json' `
  -Body '{"model":"local:default","agent":"in_app_assistant","input":"How long is the chair warranty?"}').content
```

Expected: `5 years`, taken from the FAQ.

**Idempotency:** send the same body twice with `"idempotency_key":"k-123"`. You get the same
`run_id` both times. Reusing the key with a different input returns `409 idempotency_mismatch`.

**Cancellation:** `POST /api/v1/runs/{id}/cancel` with body `{"reason":"..."}`. If the run is still
running, it moves to `cancelled` within about a second, even in the middle of a model call. If it
has already finished, cancel is a no-op and returns the final status.

**Register your own agent:** copy [examples/agents/in_app_assistant.yaml](../examples/agents/in_app_assistant.yaml),
change `metadata.id`, and upload it:

```powershell
Invoke-RestMethod -Method Post "$BASE/api/v1/agents" -Headers $H -ContentType 'application/yaml' -InFile .\my_agent.yaml
```

Registration validates the schema, node references, reachability, loops (every cycle must pass
through a `loop` node), CEL expressions and declared permissions. It rejects bad graphs with
`400 invalid_graph` and explains why. Versions are immutable: to change content, bump
`metadata.version`.

### 5.9 Crash recovery and "needs attention"

These scenarios need a crash injected at a precise moment, so they are covered by automated tests
rather than manual steps:

```powershell
.\.venv\Scripts\python.exe -m pytest runtime\tests\test_engine.py -k "resume or crash or ambiguous or stale or revoked" -v
```

What they prove:
- A worker killed between nodes resumes without repeating a node.
- A worker killed after a write reuses the recorded outcome and never writes twice.
- A worker killed after dispatching a write, but before the result came back, leaves the run in
  `needs_attention` until an operator resolves it.
- A stale worker cannot commit.
- A revoked key stops its runs.

The operator commands for resolving a run are in
[docs/runbooks/local-development.md](runbooks/local-development.md).

### 5.10 Dry-run (executes nothing)

```powershell
$d = Invoke-RestMethod -Method Post "$BASE/api/v1/dry-run" -Headers $H -ContentType 'application/json' -Body '{"agent_id":"report_generator","mode":"static"}'
"valid=$($d.valid) executed_nothing=$($d.executed_nothing)"; $d.branches; $d.permissions_required; $d.warnings
$body = @{ agent_id='report_generator'; mode='simulate'; input=@{report_name='sim'}
  fixtures=@{ sales=@{columns=@('region'); rows=@(,@('North'))}; analyse=@{title='Simulated'; body='- ok'} } } | ConvertTo-Json -Depth 8
$sim = Invoke-RestMethod -Method Post "$BASE/api/v1/dry-run" -Headers $H -ContentType 'application/json' -Body $body
$sim.steps | ForEach-Object { "$($_.node_id): $($_.status)" }; $sim.unresolved
```

Expected for the static plan: `valid=True executed_nothing=True`, one branch, the required
permissions, and a warning that `write` is a write tool.

Expected for the simulation: sales, analyse and render are `simulated`, and `write` is
`unresolved` with `no fixture ... simulation stops here`. Missing data is reported, never invented.
Add a `write` fixture and the simulation reaches `done`.

Check that nothing was written: `Test-Path examples\workspace\reports\sim.md` returns `False`.

### 5.11 SDKs (your original examples)

```powershell
.\.venv\Scripts\python.exe examples\python\quickstart.py
node examples\typescript\quickstart.mjs
```

Expected: an explanation of quantum computing, the assistant answering "30 days", streamed
planets, and (for Python) `dry-run valid: True`.

### 5.12 Observability

```powershell
(Invoke-WebRequest "$BASE/metrics").Content -split "`n" | Select-String '^uar_(model_tokens|runs|tool_calls)_total'
```

Expected: token, run and tool-call counters. With option **B**, open http://localhost:16686,
choose service `uar` and operation `agent.run`. Each agent call is one trace: HTTP request, then
`agent.run`, then one span per node, then `model.chat` and `tool.execute`, then the MCP client
span.

### 5.13 Local-only profile (no cloud egress)

In `.local\uar.yaml`, set `profile: local-only` and restart. Any `cloud:` model now fails with
`403 egress_denied`, even for an admin key:

```powershell
$A = @{ 'X-API-Key' = (($creds | Select-String '^UAR_ADMIN_KEY=').Line -split '=',2)[1] }
try { Invoke-RestMethod -Method Post "$BASE/api/v1/inference" -Headers $A -ContentType 'application/json' -Body '{"model":"cloud:default","input":"x"}' } catch { $_.ErrorDetails.Message }
```

Set it back to `standard` afterwards.

---

## 6. Automated test suites

Postgres from setup step 4 must be running. Each test session creates and drops its own temporary
database, so your data isn't touched.

| Suite | Command | Needs | Expected |
|---|---|---|---|
| Contracts current and compatible | `.\.venv\Scripts\python.exe scripts\gen_contracts.py --check; .\.venv\Scripts\python.exe scripts\check_breaking.py; .\.venv\Scripts\python.exe scripts\gen_ts_types.py --check` | — | 3 "up to date / no breaking changes" lines |
| **Runtime (main suite)** | `.\.venv\Scripts\python.exe -m pytest` | Postgres | **103 passed**, 7 deselected (~20 s) |
| Live local models | `.\.venv\Scripts\python.exe -m pytest -m live -o addopts= -rs runtime\tests\test_live.py` | Ollama; LM Studio optional; llama.cpp needs `$env:UAR_LLAMACPP_KEY` | 5 passed, 1 skipped without the llama.cpp key (up to ~5 min when LM Studio loads the 27B model) |
| Container sandbox | `.\.venv\Scripts\python.exe -m pytest -m docker -o addopts= runtime\tests\test_mcp.py` | image `uar-runtime:0.9.0` | 1 passed |
| Python SDK vs mock | `.\.venv\Scripts\python.exe -m pytest -o addopts= sdks\python\tests` | — | 10 passed |
| TypeScript SDK vs mock | `cd sdks\typescript; npm test; cd ..\..` | built SDK | 9 pass |
| **End to end against a running deployment** | `.\.venv\Scripts\python.exe scripts\smoke_test.py` | running runtime + Ollama | 27/27 |

What the main suite covers, file by file:

| File | Covers |
|---|---|
| `test_api.py` | Authentication on HTTP, gRPC and WebSocket; RBAC; tenant isolation; JWT; limits; rate limiting; fail-closed audit; identical results across transports; streaming; disconnects don't regenerate; routing, fallback, circuit breaker, budgets, ledger |
| `test_adapters.py` | Anthropic (official SDK), OpenAI Responses, OpenAI-compatible and Ollama adapters against each API's documented wire format |
| `test_mcp.py` | Real MCP servers: traversal, junction, ADS and NUL, write policy, exclusive create, read-only SQL, schema validation, crash recovery, output cap, idempotency, SSRF, Streamable HTTP |
| `test_engine.py` | Graph validation, report agent, events and resume, loops and limits, parallel, sub-agent permissions, cancellation, crash recovery, fencing, revocation |
| `test_dryrun.py` | Import boundary, tripwires that prove nothing executes, unresolved fixtures, denials, branch enumeration |
| `test_observability.py` | One connected trace per run, metrics equal the ledger, simulated policy decisions |
| `test_contracts.py`, `test_conformance.py`, `test_sdk_live.py` | Generated contract, OpenAPI coverage, shared fixtures against the runtime, both SDKs against the live runtime |

Run one file or one test like this:
`.\.venv\Scripts\python.exe -m pytest runtime\tests\test_mcp.py -k traversal -v`

---

## 7. Testing a change

### The standard loop

1. **Make the change.**
2. **Run the checks for that area** (table below).
3. **Run the main suite:** `.\.venv\Scripts\python.exe -m pytest`
4. **Restart the runtime you are testing.** With option A, `Ctrl+C` then `uar serve` again. With
   option B, rebuild and restart:

   ```powershell
   docker build -f deploy/docker/Dockerfile -t uar-runtime:0.9.0 .
   docker compose -f deploy/docker/compose.yaml --profile full up -d uar
   ```

5. **Run the end-to-end check:** `.\.venv\Scripts\python.exe scripts\smoke_test.py`
6. For changes to routing or adapters, also run the **live suite** (part 6).

### What to run for each kind of change

| You changed | Also run | Watch for |
|---|---|---|
| `proto/uarpb/**/*.proto` | `scripts\gen_contracts.py`, `scripts\gen_ts_types.py`, then `scripts\check_breaking.py`; rebuild the TS SDK; both SDK suites | A breaking change needs a new API version; `check_breaking.py --update` only for an intentional break |
| `contracts/fixtures/*.json` | `test_conformance.py`, both SDK suites | Fixtures must match the live runtime and the mock |
| `router/` or an adapter | `test_api.py`, `test_adapters.py`, live suite | Usage, cost and fallback behaviour; no retry after the first streamed token |
| `mcp/` or `mcp-servers/` | `test_mcp.py`, docker suite; restart the runtime so servers restart | Security tests must still fail closed |
| `engine/` | `test_engine.py`, `test_observability.py` | Recovery tests: never repeat a write |
| `simulation/` | `test_dryrun.py` | Must not import execution modules (the import-boundary test) |
| `governance.py`, `config.py` | `test_api.py`, `uar check-config --config .local\uar.yaml` | Audit must stay fail-closed |
| `gateway/` | `test_api.py`, `test_conformance.py`, `test_sdk_live.py`, smoke test | All three transports behave the same |
| SDK code | That SDK's suite, then `test_sdk_live.py` | No automatic retry without an idempotency key |
| `requirements.lock`, `Dockerfile` | Rebuild the image, docker suite, **smoke test against Compose** | The in-process suite cannot catch missing runtime packages |
| Helm chart | Part 8 | — |
| An example agent YAML | `test_contracts.py::test_example_agents_compile`; then a static dry-run of it | Bump `metadata.version` if it was registered before |

### Pre-merge checklist

- [ ] The three contract checks are up to date and show no breaking change.
- [ ] Main suite: all passed.
- [ ] Python and TypeScript SDK suites pass.
- [ ] Image rebuilt, docker suite passes, and `smoke_test.py` shows 27/27 against Compose.
- [ ] Live suite passes for any model or adapter change.
- [ ] [docs/gates/mvp-gates.md](gates/mvp-gates.md) and [docs/capability-matrix.md](capability-matrix.md)
      updated if a capability's status changed.

CI ([.github/workflows/ci.yml](../.github/workflows/ci.yml)) runs the contract checks, both SDKs,
the main suite, the image build and sandbox test, and `helm lint`. It has not run yet, because
the repository has no remote.

---

## 8. Kubernetes (Docker Desktop)

Status: **not verified yet.** Follow [docs/runbooks/docker-desktop.md](runbooks/docker-desktop.md).
In short:

1. Docker Desktop → Settings → Kubernetes → Enable. Then run `winget install Helm.Helm`.
2. Set `OLLAMA_HOST=0.0.0.0` for Ollama so pods can reach it, then restart Ollama.
3. Stop Compose (part 10) so the ports are free for port-forwarding.
4. Install:

   ```powershell
   docker build -f deploy/docker/Dockerfile -t uar-runtime:0.9.0 .
   .\.venv\Scripts\python.exe scripts\bootstrap_local.py --k8s
   kubectl create namespace uar-system
   helm install uar deploy/helm/uar -n uar-system --set-file config=.local/uar-k8s.yaml
   kubectl -n uar-system rollout status deploy/uar-runtime
   kubectl -n uar-system port-forward svc/uar-runtime 9000:9000 50051:50051
   ```

5. In another window, run `.\.venv\Scripts\python.exe scripts\smoke_test.py`. Expect 27/27.
6. Test upgrade and rollback (`helm upgrade ... --set runtime.replicas=2`, `helm rollback uar 1`),
   pod restart during a run, and `helm uninstall`. Check the volume-retention behaviour described
   in the runbook.

Record the results in [docs/gates/mvp-gates.md](gates/mvp-gates.md) under M6.

---

## 9. Troubleshooting

| Symptom | Fix |
|---|---|
| `database unavailable` at startup | `docker compose -f deploy/docker/compose.yaml up -d postgres` |
| `readyz` shows `"fs": false` or `"db": false` | Run the fixtures script (setup step 2); with option A check the `uar serve` log; with option B, `docker logs uar-uar-1` |
| `404` for `local:default` | Ollama isn't running or `granite4:latest` isn't pulled, or change the alias in `.local\uar.yaml` |
| `503 unavailable` right after provider failures | A circuit breaker is open; it closes again after `cooldown_s` (30 s) |
| `llamacpp: provider rejected credentials` | `llama-server` uses `--api-key`: set `$env:UAR_LLAMACPP_KEY` before `uar serve` |
| WebSocket 404 on the container | The image was built from an old lock file; rebuild it (`websockets` must be in `requirements.lock`) |
| `403 egress_denied` | `profile: local-only` or the model class isn't allowed |
| `429 budget_exceeded` | The tenant's `tokens_per_day` is used up; raise it in `.local\uar.yaml` and restart |
| Cost missing from `usage` | No price is configured for that `provider/model`. Unknown cost is never reported as zero |
| `409` on agent registration | That version already exists with different content; bump `metadata.version` |
| Tests hang or fail to connect to Postgres | Postgres must be on `127.0.0.1:55440`, or set `UAR_TEST_PG=postgresql://user:pass@host:port/postgres` |
| LM Studio live test is slow | The 27B model loads on demand the first time; it unloads after 2 idle minutes |

---

## 10. Stop, reset and clean up

```powershell
docker compose -f deploy/docker/compose.yaml --profile full down        # stop; keeps the database volume
docker compose -f deploy/docker/compose.yaml --profile full down -v     # stop and DELETE the database
Remove-Item examples\workspace\reports\*.md                              # delete generated reports
.\.venv\Scripts\python.exe scripts\bootstrap_local.py --force           # new API keys (old ones stop working)
```
