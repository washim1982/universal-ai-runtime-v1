# Inference samples: one small project per language

Each sample is one page of code that does the same three things with the SDK for its language:

1. **Signs in**: as a registered application (client ID + secret, exchanged for an access token at
   the token service), or with an API key.
2. **Asks one question** and prints the complete answer, the model that answered and the tokens used.
3. **Asks it again as a stream** and prints the answer token by token.

| Language | Folder | Code | Run |
|---|---|---|---|
| Python 3.12+ | [python](python) | [inference.py](python/inference.py) | `pip install -r requirements.txt` then `python inference.py` |
| TypeScript (Node 22.18+) | [typescript](typescript) | [inference.ts](typescript/inference.ts) | `npm install` then `node inference.ts` |
| Go 1.27 | [go](go) | [main.go](go/main.go) | `go run .` |
| Rust | [rust](rust) | [src/main.rs](rust/src/main.rs) | `cargo run` |
| C# / .NET 8+ | [dotnet](dotnet) | [Program.cs](dotnet/Program.cs) | `dotnet run` |
| Java 17+ (Maven) | [java](java) | [Inference.java](java/src/main/java/Inference.java) | `mvn -f ../../sdks/java install -DskipTests` once, then `mvn -q compile exec:java` |

Each project uses the SDK from this repository (`sdks/<language>`), so no package has to be published.
Pass your own question as arguments, for example `go run . "Summarise the plot of Hamlet"`
(Java: `-Dexec.args="…"`, .NET: `dotnet run -- "…"`, Rust: `cargo run -- "…"`).

## Before you run

1. The runtime is running: UAR Admin → Overview shows **Running**, or `http://127.0.0.1:9000/healthz`
   answers `{"status":"ok"}`.
2. Choose how the sample signs in, in the same terminal:

**Recommended: a registered application.** In UAR Admin open **Applications**, **Register
application** with the role *developer*, and copy the client ID and secret. Then:

```powershell
$env:UAR_CLIENT_ID = "<client id>"
$env:UAR_CLIENT_SECRET = "<client secret>"
```

**Or: an API key** (for example the developer key in `.local\credentials.env`):

```powershell
$env:UAR_API_KEY = "<api key>"
```

When both are set, the application credentials win.

Optional settings:

| Variable | Default | Meaning |
|---|---|---|
| `UAR_URL` | `http://127.0.0.1:9000` | Where the runtime is |
| `UAR_MODEL` | `local:default` | Model to use: an alias, `local:ollama/granite4`, `cloud:anthropic/claude-sonnet-5` (if your tenant may use cloud models) … UAR Admin → API reference → `GET /api/v1/models` lists them |

## What you should see

```text
UAR http://127.0.0.1:9000 | model local:default | signed in with application uar-samples-cdb3a4ba

[ollama/granite4:latest] The capital of France is Paris.
tokens: 18 in, 8 out

streaming: The capital of France is Paris.
```

All six samples were run against a local runtime (Ollama, granite4) with both an application and
an API key, and produce this output.

## How the sign-in works in each language

- **Python and TypeScript**: the SDK does it. `Client(url)` / `new UAR(url)` reads
  `UAR_CLIENT_ID` + `UAR_CLIENT_SECRET` (or `UAR_API_KEY`), gets an access token, renews it before
  it expires and retries once if the runtime rejects it (for example after a key rotation).
- **Go, Rust, C# and Java**: the sample has a short `accessToken` function that performs the
  OAuth 2.0 client-credentials request (`POST /api/v1/oauth/token`) and gives the token to the SDK
  client as a bearer token. A token lasts 15 minutes by default; a long-running program should
  request a new one before `expires_in` runs out.

## If it fails

| Message | Meaning |
|---|---|
| `401 invalid_client` | Wrong client ID or secret, the secret was revoked or expired, or the application was disabled |
| `401 unauthenticated` | Wrong API key, or the runtime accepts tokens only (`sts.require_tokens`) |
| `403 permission_denied` | The application or key lacks the role for this model class (e.g. `inference:cloud`) |
| `404 not_found` | The model name is unknown; check `UAR_MODEL` |
| `503 unavailable` / connection refused | The runtime or the model provider (e.g. Ollama) is not running |

More about applications and tokens: [docs/sts.md](../docs/sts.md).
