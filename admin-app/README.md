# UAR Admin: Windows administration app

A native Windows app (WPF, .NET 8) for everyday administration of a Universal AI Runtime.

| Page | What you can do |
|---|---|
| **Overview** | See if the runtime is up, its version, uptime and schema; **Start**, **Stop**, **Restart**; health of every component (database, model providers, MCP tool servers, plugins, SSO, worker); approvals waiting and the last 24 hours of model calls, tokens and cost |
| **Access & keys** | List the tenant's API keys (config-file keys read-only); **create** a key with chosen roles and expiry (the secret is shown once); **revoke** a key (effective at once); roles and their permissions; SSO group and service-account mappings |
| **API reference** | Every HTTP endpoint from the live `openapi.json`, with request and response shapes, a **Try it** panel that calls the runtime with your connection's key, and a ready-made `curl` command |
| **Inference history** | Every model call of the tenant (who, provider, model, tokens, cost, run), filters, totals, CSV export. Prompts and outputs are never stored or shown |
| **Approvals** | Runs paused for a human decision, with the exact action and arguments; **Approve** or **Reject** with a comment. The decision is bound to the arguments you saw |
| **Audit trail** | The tenant's tamper-evident audit log, filter, entry details with hashes, **Verify chain**, export as JSON Lines or CSV |
| **Service logs** | Live runtime logs (via the API), the console output of a runtime started by the app, or the Docker container's log; level filter, search, pause, save |
| **Settings** | Connections (runtime URL + API key, one per tenant), the runtime folder for Start/Restart, refresh interval |

## Run it

From the project folder, double-click **`admin.cmd`**. It builds the app the first time
(needs the .NET SDK 8 or newer) and starts `admin-app\dist\UarAdmin.exe`.

On first start the app finds the project folder and imports the local admin key from
`.local\credentials.env` (created by the setup dashboard), so it connects to the local runtime
at `http://127.0.0.1:9000` without any typing. To manage another runtime or tenant, add a
connection in **Settings** with that runtime's URL and an admin key.

Other ways to build:

```bat
admin-app\build.cmd                    :: small exe, needs the .NET 8+ Desktop Runtime
admin-app\build.cmd --self-contained   :: about 70 MB, runs on any 64-bit Windows 10/11
dotnet run --project admin-app\UarAdmin
```

The runtime must be version 0.9 or newer (the admin API). If you run the Docker image, rebuild it
after updating the project: `docker build -f deploy/docker/Dockerfile -t uar-runtime:0.9.0 .`.

## Start, stop and restart

| The runtime was started… | Start / Stop / Restart does |
|---|---|
| by UAR Admin | runs `.venv\Scripts\python.exe -m uar_runtime.main serve --config .local\uar.yaml` in the project folder, captures its output (Service logs → Process output, and `%LOCALAPPDATA%\UAR\Admin\logs`), and stops it with its whole process tree. Closing the app stops it too (you are asked first) |
| in a terminal or by the setup dashboard | Stop ends that process (you confirm first). Restart starts it again under UAR Admin |
| by Docker Compose | Stop and Start use `docker stop` / `docker start` on the container that publishes the port |
| on another machine | Start/Stop are disabled; everything else works over the API |

## Security

- The app only uses the runtime's authenticated API; it has no database access. What a connection
  can do is exactly what its API key's roles allow in that tenant.
- Keys are saved in `%APPDATA%\UAR\Admin\settings.json` encrypted with Windows DPAPI for your
  Windows account; copying the file to another account or PC does not reveal them.
- Created keys are shown once. The runtime stores only their SHA-256.
- Service logs and component details are process-wide, so the runtime shows them only to admins of
  the tenants listed in `admin.platform_tenants`.
- Approve/Reject sends the arguments hash that was displayed. If the arguments changed, the runtime
  refuses the decision.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Status "offline" | Start the runtime (Overview → Start) or check the connection URL in Settings |
| "online · key not admin" | The connection's key lacks the admin role. Add a connection with an admin key |
| Pages show 404 errors | The runtime is older than 0.9. Update it; for Docker, rebuild the image |
| Start fails: "Python environment not found" | Run the setup dashboard (`setup.cmd`) once to create `.venv` and `.local\uar.yaml` |
| Start fails: "port 9000 is already used" | Another runtime (often the Docker one) is running. Stop it first, or connect to it instead |
| Service logs: "only administrators of the platform tenants" | Add your tenant to `admin.platform_tenants` in the runtime configuration |

For support diagnostics, `UarAdmin.exe --snapshot <folder>` renders every page to PNG files and exits.
