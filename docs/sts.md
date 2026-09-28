# Application registration and access tokens (STS)

The runtime includes a security token service (STS). Instead of sharing long-lived API keys,
register each program as an **application**. The application exchanges its **client ID and client
secret** for a short-lived **access token**, and calls inference or any other endpoint with it.

```text
 admin ── register app ──► runtime                     (once: client_id + client_secret, shown once)
 app   ── client_id + secret ──► POST /api/v1/oauth/token   ──►  access_token (15 min, ES256 JWT)
 app   ── Authorization: Bearer <access_token> ──► /api/v1/inference, /api/v1/agent/run, …
```

It is standard OAuth 2.0 (RFC 6749 section 4.4, `client_credentials`), so any OAuth client library
works: the discovery document is at `/.well-known/openid-configuration` and the public keys at
`/.well-known/jwks.json`.

## 1. Register an application

**UAR Admin:** open **Applications**, click **Register application**, choose a name, the roles its
tokens may carry, and the token lifetime. Copy the client secret from the dialog; it is shown once.
**Get a token and test it** checks the credentials against the live runtime.

**API** (needs `apps:manage`, which admins have):

```powershell
$admin = @{ "X-API-Key" = "<admin key>" }
Invoke-RestMethod -Method Post http://127.0.0.1:9000/api/v1/admin/apps -Headers $admin `
  -ContentType "application/json" -Body '{"name":"Billing Service","roles":["developer"],"token_ttl_s":900}'
```

Response: `client_id`, `client_secret` (once), `token_url`, and the registration.

**Command line on the server** (bootstrap when API keys are turned off, see section 4):

```bash
uar register-app --config .local/uar.yaml --tenant acme --name "Ops Console" --roles admin
```

## 2. Get an access token

```powershell
$t = Invoke-RestMethod -Method Post http://127.0.0.1:9000/api/v1/oauth/token -Body @{
  grant_type = "client_credentials"; client_id = "<client id>"; client_secret = "<client secret>" }
$t.access_token   # valid for $t.expires_in seconds
```

```bash
curl -u "<client id>:<client secret>" -d grant_type=client_credentials http://127.0.0.1:9000/api/v1/oauth/token
```

Both `client_secret_basic` (HTTP Basic) and `client_secret_post` (form fields) work, form-encoded or
JSON. Add `scope=viewer` (space-separated roles) to get a token with fewer roles than the
application has. Errors follow RFC 6749: `invalid_client` (401), `invalid_scope` (400),
`unsupported_grant_type` (400); ten failed attempts per client in a minute are throttled (429).

## 3. Call any endpoint

```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:9000/api/v1/inference -Headers @{ Authorization = "Bearer $($t.access_token)" } `
  -ContentType "application/json" -Body '{"model":"local:default","input":"Hello"}'
```

The SDKs do the token exchange and renewal for you:

```python
from uar import Client
client = Client("http://127.0.0.1:9000", client_id="billing-service-1a2b3c4d", client_secret=SECRET)
print(client.inference(model="local:default", prompt="Hello").text)
```

```ts
const client = new UAR("http://127.0.0.1:9000", { clientId: "billing-service-1a2b3c4d", clientSecret: SECRET });
```

(`UAR_CLIENT_ID` and `UAR_CLIENT_SECRET` are read from the environment when no credential is passed.)
gRPC clients send the same `authorization: Bearer …` metadata. In the runtime the caller appears as
`app:<client id>`, in the audit trail and the inference history.

## 4. Operating it

| Task | How | Effect |
|---|---|---|
| Rotate a client secret | **New client secret**, update the application, then **Revoke** the old one | Two secrets can be active at once, so there is no downtime. Tokens already issued stay valid until they expire |
| Remove an application | **Disable application** | Its tokens stop working **immediately** (other replicas within `admin.key_refresh_s`), no new tokens |
| Reduce what an app may do | Register a new app with fewer roles and disable the old one | Tokens carry at most the app's roles, re-checked on every request |
| Rotate the signing key | **Rotate signing key** (platform administrators) | A new key is published, then signs after `sts.key_activation_delay_s`; tokens from the old key stay valid until they expire |
| Turn API keys off | `sts.require_tokens: true` | Every call needs an STS (or OIDC) token; register the first admin application with `uar register-app` |
| Encrypt signing keys at rest | `sts.key_encryption_env: UAR_STS_KEK` and set that variable | Private keys in PostgreSQL are encrypted; without the variable the runtime cannot sign |

Configuration (`sts:` in the runtime configuration): `enabled`, `issuer` (default `urn:uar:sts`),
`audience` (`uar`), `default_token_ttl_s` (900), `max_token_ttl_s` (3600), `require_tokens`,
`key_encryption_env`, `key_activation_delay_s`, `max_failed_attempts`. Set `server.public_url` when
clients reach the runtime through another address (the token URL shown to applications uses it).

## Security properties

- Client secrets are 256-bit random values; only their SHA-256 is stored. Secrets can expire.
- Access tokens are ES256-signed JWTs (`typ: at+jwt`) with `iss`, `aud`, `sub` (client id),
  `uar_tenant`, `roles`, `exp` and a unique `jti`. Only ES256 is accepted: tokens using `none`,
  HMAC or another key are refused, as are tampered, expired, wrong-audience and retired-key tokens.
- The tenant comes from the application's registration, never from the request.
- Registering, rotating, revoking, disabling, every issued token and every failed attempt are in
  the tamper-evident audit trail.
- A token cannot be revoked individually before it expires, except by disabling its application;
  keep lifetimes short (default 15 minutes).
