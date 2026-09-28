# Inference guardrails

Guardrails are security checks the runtime applies to **every model call**: the inference API
(synchronous and streaming), model steps inside agent runs, and automatic tool-calling loops.

| Stage | What is checked | Why |
|---|---|---|
| `input` | the caller's messages (user and assistant turns) | jailbreaks, prompt injection, sensitive data sent to a model |
| `tool_results` | tool output fed back to the model | **indirect** prompt injection hidden in documents, web pages, tickets |
| `output` | the model's answer, including streams | leaks of personal data, card data and secrets |

System messages (the application's or agent's own instructions) are trusted and not checked.

## Checks

| Check | Detects | Notes |
|---|---|---|
| `prompt_injection` | instruction overrides ("ignore previous instructions"), system-prompt extraction, jailbreak personas (DAN, developer mode), "no restrictions" requests, spoofed role markers (`<\|im_start\|>system`, `[INST]`, `### system:`), exfiltration and tool-hijack phrasing, **invisible Unicode tag characters** ("ASCII smuggling"), zero-width and bidi tricks, large encoded blobs | Weighted heuristics give a score 0..1; the check fires at `threshold`. Optional classifier model for borderline text (`model`, `classifier_min_score`) |
| `pii` | `email`, `phone`, `us_ssn`, `iban`, `ipv4` | |
| `pci` | `card_number` (Luhn-checked, so order numbers don't trigger it), `cvv` (next to "CVV/CVC/security code"), `track_data` (magnetic stripe) | |
| `secrets` | `private_key`, `aws_access_key`, `github_token`, `slack_token`, `anthropic_key`, `openai_key`, `google_api_key`, `uar_key`, `jwt`, `connection_string`, `password_assignment`, `generic_api_key` | |
| `denied_terms` | your own words/phrases (`terms`) and regular expressions (`patterns`) | e.g. project codenames, competitor names |
| `max_chars` | oversized input | always blocks |

Text is normalised before matching (compatibility characters, zero-width characters), so
`jane​.doe@exa​mple.com` or a card number in full-width digits is still found.
`types:` limits a check to some detectors, e.g. `pii: {action: redact, types: [email, us_ssn]}`.

## Actions

| Action | Effect |
|---|---|
| `block` | The call is rejected with **`guardrail_blocked`** (HTTP 422, gRPC INVALID_ARGUMENT). An input block means the model is never called. `details.findings` names the checks; the offending text is never echoed |
| `redact` | Matches are replaced with `[redacted:<type>]` and the call continues: the model never sees the value (input), or the caller never does (output) |
| `flag` | The call continues unchanged; the finding is reported and audited |
| `off` | The check does not run |

Findings are returned in the response (`guardrails` on `InferenceResponse` and on a stream's
`completed` event), written to the **audit trail** (action `guardrail`, outcome
`blocked`/`redacted`/`flagged`; check, type and count only, never the content) and counted in the
Prometheus metric `uar_guardrail_findings_total{stage,check,action}`. In agent runs a block fails
the model step with `guardrail_blocked`.

**Streaming:** when an output check redacts or blocks, the runtime holds back the most recent ~100
characters and never releases text in the middle of a match, so a secret split across chunks is
still caught. A block ends the stream with an `error` event and stops the provider request.

## Configuration

```yaml
guardrails:
  enabled: true
  policy:
    input:
      prompt_injection: {action: block, threshold: 0.7}
      #   model: local:default             # optional classifier (asked when the score is in [0.3, 0.7))
      pii: {action: flag}
      pci: {action: block}
      secrets: {action: block}
      denied_terms: {action: block, terms: ["Project Falcon"], patterns: ["\\bACME-\\d{4}\\b"]}
      max_chars: 100000
    tool_results:
      prompt_injection: {action: block, threshold: 0.8}
      secrets: {action: redact}
    output:
      pii: {action: flag}
      pci: {action: redact}
      secrets: {action: redact}
    exempt_roles: [red_team]               # principals with these roles skip the checks
  tenants:                                 # a tenant's policy replaces the default one
    healthco:
      input: {pii: {action: redact}, prompt_injection: {action: block}}
      output: {pii: {action: redact}}
```

Guardrails are off unless `enabled: true`; the example configuration enables the policy above.
Restart the runtime after changing it.

## Testing a policy

- **UAR Admin → Guardrails**: the policy in force, a test console (paste text or use the examples,
  choose prompt / tool result / answer) and the recent guardrail events from the audit trail.
- **API** (permission `guardrails:check`, which developers have): runs the caller's policy over a
  text without calling a model.

```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:9000/api/v1/guardrails/check -Headers @{ "X-API-Key" = $key } `
  -ContentType "application/json" -Body '{"text":"card 4111 1111 1111 1111","stage":"input"}'
# allowed: False · action: block · findings: [{check: pci, type: card_number, action: block, count: 1}]
```

## Limits

Detection is pattern- and heuristic-based. It stops common attacks and leaks, but a determined
attacker can phrase an injection that scores low, and data formats outside the detector list pass.
Use guardrails together with the other controls: least-privilege tool policies, human approvals for
writes (`require_approval`), egress redaction to cloud models, and the audit trail. The optional
classifier model adds a second opinion for borderline prompts, at the cost of one extra model call.
