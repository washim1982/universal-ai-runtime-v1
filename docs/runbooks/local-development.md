# Runbook: local development and troubleshooting

## Everyday loop

```bash
docker compose -f deploy/docker/compose.yaml up -d postgres
.venv/Scripts/uar serve --config .local/uar.yaml          # HTTP :9000, gRPC :50051
.venv/Scripts/python -m pytest                              # ~12 s
```

After editing a `.proto`: `python scripts/gen_contracts.py && python scripts/gen_ts_types.py`, then
`python scripts/check_breaking.py`. Accept an intentional breaking change only with a new API
version; `--update` moves the baseline.

## Registering and running agents

```bash
curl -s localhost:9000/api/v1/agents -H "X-API-Key: %UAR_DEV_KEY%" -H "Content-Type: application/yaml" --data-binary @examples/agents/report_generator.yaml
curl -s localhost:9000/api/v1/agent/run -H "X-API-Key: %UAR_DEV_KEY%" -H "Content-Type: application/json" -d "{\"agent_id\":\"report_generator\",\"input\":{\"report_name\":\"q2\"}}"
curl -N localhost:9000/api/v1/runs/<run_id>/events -H "X-API-Key: %UAR_DEV_KEY%"
```

Agents in `agents_dir` are registered for every tenant at startup. Versions are immutable: change
the content, bump `metadata.version`.

## Runs that need attention

A write tool whose outcome is unknown (worker crash or timeout after dispatch) puts the run in
`needs_attention`. Inspect the target system, then with an operator key:

```bash
curl -s localhost:9000/api/v1/runs/<run_id>/resolve -H "X-API-Key: %UAR_OPS_KEY%" -H "Content-Type: application/json" -d "{\"action\":\"retry_node\",\"note\":\"checked: nothing written\"}"
```

`mark_completed` continues as if the write happened; `fail` ends the run.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `database unavailable` at startup | Postgres not running on 127.0.0.1:55440 (`docker compose ... up -d postgres`) |
| `NotImplementedError` from psycopg on Windows | Use `uar serve` (it selects the selector event loop), not a custom asyncio runner |
| MCP server `unavailable` | Run it by hand: `python -m uar_mcp_servers.fs_server` with `UAR_FS_ROOT` set; stderr shows the error |
| `egress_denied` for a model class | Profile `local-only` disables `cloud`; or the tenant lacks `allow_cloud` |
| `llamacpp: provider rejected credentials` | `llama-server` was started with `--api-key`; set `UAR_LLAMACPP_KEY` |
| Cost missing from usage | No price configured for that `provider/model` — unknown is never reported as zero |
