# Runbook: Docker Desktop Kubernetes (local profile)

Status: **written, not yet exercised** (see `docs/gates/mvp-gates.md`, M6). Record the results of
each step there when you run it.

## Prerequisites

1. Docker Desktop → Settings → Kubernetes → **Enable Kubernetes** (kubeadm). Wait until it shows
   "running"; `kubectl config use-context docker-desktop`.
2. Helm 3 (`winget install Helm.Helm`).
3. Model servers on the host (Ollama at minimum). Pods reach them at `host.docker.internal`.

## Install

```bash
docker build -f deploy/docker/Dockerfile -t uar-runtime:0.9.0 .      # Docker Desktop's cluster sees local images
python scripts/bootstrap_local.py --k8s                                # .local/uar-k8s.yaml (reuses your keys)
kubectl create namespace uar-system
helm install uar deploy/helm/uar -n uar-system --set-file config=.local/uar-k8s.yaml
kubectl -n uar-system rollout status deploy/uar-runtime
kubectl -n uar-plugins get pods
kubectl -n uar-system port-forward svc/uar-runtime 9000:9000 50051:50051
curl http://127.0.0.1:9000/readyz
```

Smoke test (developer key from `.local/credentials.env`):

```bash
set UAR_API_KEY=<UAR_DEV_KEY>
python examples/python/quickstart.py
```

Expected: inference and streaming answers, the in-app assistant answering "30 days", and a valid
dry-run plan. Generate a report and check it landed on the workspace volume:

```bash
kubectl -n uar-plugins exec deploy/uar-mcp-fs -- ls /workspace/reports
```

## Upgrade and rollback

```bash
helm upgrade uar deploy/helm/uar -n uar-system --set-file config=.local/uar-k8s.yaml --set runtime.replicas=2
helm history uar -n uar-system
helm rollback uar 1 -n uar-system
```

Migrations run in the `migrate` init container under an advisory lock, so several replicas can
start together. Runs survive a runtime restart: kill the pod during a run
(`kubectl -n uar-system delete pod -l app.kubernetes.io/name=uar-runtime`) and the new pod reclaims
it after the lease expires (30 s) and resumes from the last checkpoint.

## Uninstall and data retention

```bash
helm uninstall uar -n uar-system
```

Kept on purpose: the Postgres PVC `data-uar-postgres-0` and its password Secret `uar-postgres`
(namespace `uar-system`), and the workspace PVC `uar-workspace` (namespace `uar-plugins`). A later
`helm install` reuses them. To discard all data:

```bash
kubectl -n uar-system delete pvc data-uar-postgres-0 && kubectl -n uar-system delete secret uar-postgres
kubectl -n uar-plugins delete pvc uar-workspace && kubectl delete namespace uar-plugins
```

## Troubleshooting

| Symptom | Check |
|---|---|
| `readyz` 503, `mcp.fs=false` | `kubectl -n uar-plugins logs deploy/uar-mcp-fs`; the runtime reaches it at `uar-mcp-fs.uar-plugins.svc.cluster.local:8765`; `egress.allow_private` must include the service CIDR (default `10.0.0.0/8`) |
| `local:default` 404 or 503 | Ollama must listen on all interfaces for pods: set `OLLAMA_HOST=0.0.0.0` on the host and restart Ollama |
| Runtime pod CrashLoop in `migrate` | Postgres not ready yet; the init container retries every 3 s |
| NetworkPolicies have no effect | Docker Desktop's default CNI does not enforce them; the chart still renders them for enforcing clusters |
