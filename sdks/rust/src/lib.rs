//! Rust client for the Universal AI Runtime (HTTP/JSON + SSE).
//!
//! ```no_run
//! # async fn demo() -> Result<(), uar::Error> {
//! let client = uar::Client::new("http://localhost:9000"); // key from UAR_API_KEY
//! let resp = client.inference("local:default", "Explain quantum computing", Some("research_agent")).await?;
//! println!("{}", resp.text());
//! # Ok(()) }
//! ```
//!
//! Only GET requests and requests with an idempotency key are retried (429, 503, network errors);
//! inference and tool calls without a key never are.

use std::time::Duration;

use bytes::Bytes;
use futures_util::{Stream, StreamExt};
use serde::{Deserialize, Serialize};
use serde_json::{json, Map, Value};

pub const VERSION: &str = "0.8.0";

/// A structured runtime error, or a transport/decoding failure.
#[derive(Debug, thiserror::Error)]
pub enum Error {
    #[error("{code}: {message} (status {status}, request {request_id})")]
    Api { status: u16, code: String, message: String, request_id: String, retryable: bool, details: Value },
    #[error("transport: {0}")]
    Transport(#[from] reqwest::Error),
    #[error("decode: {0}")]
    Decode(#[from] serde_json::Error),
}

impl Error {
    pub fn status(&self) -> Option<u16> {
        match self { Error::Api { status, .. } => Some(*status), _ => None }
    }
    pub fn code(&self) -> Option<&str> {
        match self { Error::Api { code, .. } => Some(code), _ => None }
    }
}

macro_rules! message {
    ($(#[$m:meta])* $name:ident { $($field:ident : $ty:ty),* $(,)? }) => {
        $(#[$m])*
        #[derive(Debug, Clone, Default, Serialize, Deserialize)]
        pub struct $name {
            $( #[serde(default, skip_serializing_if = "is_default")] pub $field: $ty, )*
            /// Fields not modelled above are kept, so a response round-trips exactly.
            #[serde(flatten)]
            pub extra: Map<String, Value>,
        }
    };
}

fn is_default<T: Default + PartialEq>(v: &T) -> bool { *v == T::default() }

message!(
    /// uar.v1.InferenceResponse
    InferenceResponse { request_id: String, provider: String, model: String, content: String,
                        finish_reason: String, run_id: String, tool_calls: Vec<Value>, usage: Option<Value>,
                        route: Option<Value>, output: Option<Value> });
impl InferenceResponse {
    /// The generated text (alias of `content`).
    pub fn text(&self) -> &str { &self.content }
}

message!(
    /// uar.v1.Run
    Run { run_id: String, agent_id: String, version: String, status: String, steps: i64, current_node: String,
          output: Option<Value>, error: Option<Value>, usage: Option<Value>, created_at: String, updated_at: String,
          parent_run_id: String, cancel_requested: bool });

message!(
    /// uar.v1.ToolResult
    ToolResult { request_id: String, tool: String, is_error: bool, content: Vec<Value>, structured: Option<Value>,
                 truncated: bool, untrusted: bool, duration_ms: i64 });

message!(
    /// uar.v1.DryRunReport
    DryRunReport { request_id: String, mode: String, valid: bool, errors: Vec<String>, warnings: Vec<String>,
                   steps: Vec<Value>, routes: Vec<Value>, unresolved: Vec<String>, branches: Vec<String>,
                   executed_nothing: bool });

message!(
    /// uar.v1.AgentVersion
    AgentVersion { agent_id: String, version: String, digest: String, created_at: String, warnings: Vec<String> });

/// One stream event. `kind` is the populated body ("started", "token", "usage", "completed", "error", ...).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Event {
    #[serde(rename = "type", default)]
    pub kind: String,
    #[serde(default)]
    pub seq: i64,
    #[serde(flatten)]
    pub fields: Map<String, Value>,
}

impl Event {
    /// The event body, e.g. {"text": "..."} for a token.
    pub fn body(&self) -> Option<&Value> { self.fields.get(&self.kind) }
    /// Token text for `token` events.
    pub fn token(&self) -> Option<&str> { self.body()?.get("text")?.as_str() }
}

/// Optional inference settings.
#[derive(Debug, Clone, Default)]
pub struct InferenceOptions {
    pub messages: Option<Vec<Value>>,
    pub agent: Option<String>,
    pub tools: Option<Vec<String>>,
    pub tool_mode: Option<String>,
    pub temperature: Option<f64>,
    pub max_tokens: Option<u32>,
    pub response_schema: Option<Value>,
    pub data_class: Option<String>,
    pub extensions: Option<Value>,
}

fn inference_body(model: &str, prompt: &str, o: &InferenceOptions, stream: bool) -> Value {
    let mut b = Map::new();
    b.insert("model".into(), json!(model));
    match &o.messages {
        Some(m) if !m.is_empty() => { b.insert("messages".into(), json!(m)); }
        _ => { b.insert("input".into(), json!(prompt)); }
    }
    if let Some(v) = &o.agent { b.insert("agent".into(), json!(v)); }
    if let Some(v) = &o.tools { b.insert("tools".into(), json!(v)); }
    if let Some(v) = &o.tool_mode { b.insert("tool_mode".into(), json!(v)); }
    if let Some(v) = &o.data_class { b.insert("data_class".into(), json!(v)); }
    if let Some(v) = &o.extensions { b.insert("extensions".into(), v.clone()); }
    let mut p = Map::new();
    if let Some(v) = o.temperature { p.insert("temperature".into(), json!(v)); }
    if let Some(v) = o.max_tokens { p.insert("max_tokens".into(), json!(v)); }
    if let Some(v) = &o.response_schema { p.insert("response_schema".into(), v.clone()); }
    if !p.is_empty() { b.insert("params".into(), Value::Object(p)); }
    if stream { b.insert("stream".into(), json!(true)); }
    Value::Object(b)
}

/// Client for one runtime.
#[derive(Clone)]
pub struct Client {
    base_url: String,
    api_key: Option<String>,
    token: Option<String>,
    max_retries: u32,
    http: reqwest::Client,
}

impl Client {
    /// Uses UAR_API_KEY from the environment unless `with_api_key` is called.
    pub fn new(base_url: &str) -> Self {
        Client { base_url: base_url.trim_end_matches('/').to_string(), api_key: std::env::var("UAR_API_KEY").ok(),
                 token: None, max_retries: 2, http: reqwest::Client::new() }
    }
    pub fn with_api_key(mut self, key: impl Into<String>) -> Self {
        let k: String = key.into();
        self.api_key = if k.is_empty() { None } else { Some(k) };
        self
    }
    pub fn with_token(mut self, token: impl Into<String>) -> Self { self.token = Some(token.into()); self }
    pub fn with_max_retries(mut self, n: u32) -> Self { self.max_retries = n; self }

    fn request(&self, method: reqwest::Method, path: &str, idem: Option<&str>, accept: &str) -> reqwest::RequestBuilder {
        let mut r = self.http.request(method, format!("{}{}", self.base_url, path))
            .header("Accept", accept).header("User-Agent", format!("uar-rust/{VERSION}"));
        if let Some(k) = &self.api_key { r = r.header("X-API-Key", k); }
        else if let Some(t) = &self.token { r = r.bearer_auth(t); }
        if let Some(i) = idem { r = r.header("Idempotency-Key", i); }
        r
    }

    async fn call<T: for<'de> Deserialize<'de>>(&self, method: reqwest::Method, path: &str, body: Option<&Value>,
                                                idem: Option<&str>) -> Result<T, Error> {
        let retryable = method == reqwest::Method::GET || idem.is_some();
        let mut attempt = 0;
        loop {
            let mut rb = self.request(method.clone(), path, idem, "application/json");
            if let Some(b) = body { rb = rb.json(b); }
            let outcome = rb.send().await;
            let (status, retry_after) = match &outcome {
                Ok(r) => (r.status().as_u16(), r.headers().get("retry-after").and_then(|v| v.to_str().ok())
                    .and_then(|v| v.parse::<u64>().ok())),
                Err(_) => (0, None),
            };
            if let Ok(resp) = outcome {
                if status < 400 { return Ok(resp.json::<T>().await?); }
                if !(retryable && attempt < self.max_retries && (status == 429 || status == 503)) {
                    return Err(api_error(status, resp.bytes().await.unwrap_or_default()));
                }
            } else if let Err(e) = outcome {
                if !(retryable && attempt < self.max_retries) { return Err(e.into()); }
            }
            let base = retry_after.map(|s| s.min(30) * 1000).unwrap_or(250 * (1 << attempt));
            let jitter = 0.5 + rand::random::<f64>();
            tokio::time::sleep(Duration::from_millis((base as f64 * jitter) as u64)).await;
            attempt += 1;
        }
    }

    // ------------------------------------------------------------------ inference

    /// Synchronous inference. With `agent`, runs that agent on the prompt and returns its result.
    pub async fn inference(&self, model: &str, prompt: &str, agent: Option<&str>) -> Result<InferenceResponse, Error> {
        let o = InferenceOptions { agent: agent.map(str::to_string), ..Default::default() };
        self.inference_with(model, prompt, &o).await
    }

    pub async fn inference_with(&self, model: &str, prompt: &str, o: &InferenceOptions) -> Result<InferenceResponse, Error> {
        self.call(reqwest::Method::POST, "/api/v1/inference", Some(&inference_body(model, prompt, o, false)), None).await
    }

    /// Streaming inference: started, token..., usage, then exactly one completed | error event.
    /// Dropping the stream closes the connection, which stops generation on the server.
    pub async fn stream(&self, model: &str, prompt: &str, o: &InferenceOptions)
        -> Result<impl Stream<Item = Result<Event, Error>>, Error> {
        let body = inference_body(model, prompt, o, true);
        self.sse(self.request(reqwest::Method::POST, "/api/v1/inference", None, "text/event-stream").json(&body)).await
    }

    async fn sse(&self, rb: reqwest::RequestBuilder) -> Result<impl Stream<Item = Result<Event, Error>>, Error> {
        let resp = rb.send().await?;
        let status = resp.status().as_u16();
        if status >= 400 { return Err(api_error(status, resp.bytes().await.unwrap_or_default())); }
        let bytes = resp.bytes_stream();
        Ok(futures_util::stream::unfold((bytes, String::new(), false), |(mut bytes, mut buf, done)| async move {
            loop {
                if let Some(pos) = buf.find("\n\n") {
                    let frame: String = buf.drain(..pos + 2).collect();
                    let data: Vec<&str> = frame.lines().filter_map(|l| l.strip_prefix("data:")).map(str::trim_start).collect();
                    if data.is_empty() { continue; }
                    let ev = serde_json::from_str::<Event>(&data.join("\n")).map_err(Error::from);
                    return Some((ev, (bytes, buf, done)));
                }
                if done { return None; }
                match bytes.next().await {
                    Some(Ok(chunk)) => buf.push_str(&String::from_utf8_lossy(&chunk).replace("\r\n", "\n")),
                    Some(Err(e)) => return Some((Err(e.into()), (bytes, buf, true))),
                    None => {
                        // Connection closed: a final frame without its blank line still counts.
                        let mut rest = std::mem::take(&mut buf);
                        if rest.trim().is_empty() { return None; }
                        return Some((flush(&mut rest), (bytes, String::new(), true)));
                    }
                }
            }
        }))
    }

    // ------------------------------------------------------------------ tools & catalog

    pub async fn execute_tool(&self, tool: &str, args: Value, idempotency_key: Option<&str>) -> Result<ToolResult, Error> {
        self.call(reqwest::Method::POST, "/api/v1/tool/execute", Some(&json!({"tool": tool, "args": args})),
                  idempotency_key).await
    }

    pub async fn list_models(&self) -> Result<Vec<Value>, Error> {
        let v: Value = self.call(reqwest::Method::GET, "/api/v1/models", None, None).await?;
        Ok(v.get("models").and_then(Value::as_array).cloned().unwrap_or_default())
    }

    pub async fn list_tools(&self) -> Result<Vec<Value>, Error> {
        let v: Value = self.call(reqwest::Method::GET, "/api/v1/tools", None, None).await?;
        Ok(v.get("tools").and_then(Value::as_array).cloned().unwrap_or_default())
    }

    // ------------------------------------------------------------------ agents & runs

    pub async fn register_agent(&self, definition: Value) -> Result<AgentVersion, Error> {
        self.call(reqwest::Method::POST, "/api/v1/agents", Some(&json!({"definition": definition})), None).await
    }

    /// Start a run (202). Pass an idempotency key to make it safe to retry.
    pub async fn run_agent(&self, agent_id: &str, input: Value, idempotency_key: Option<&str>) -> Result<Run, Error> {
        self.call(reqwest::Method::POST, "/api/v1/agent/run", Some(&json!({"agent_id": agent_id, "input": input})),
                  idempotency_key).await
    }

    pub async fn get_run(&self, run_id: &str) -> Result<Run, Error> {
        self.call(reqwest::Method::GET, &format!("/api/v1/runs/{run_id}"), None, None).await
    }

    /// Poll until the run is terminal or needs attention.
    pub async fn wait_run(&self, run_id: &str) -> Result<Run, Error> {
        loop {
            let r = self.get_run(run_id).await?;
            if matches!(r.status.as_str(), "succeeded" | "failed" | "cancelled" | "needs_attention") { return Ok(r); }
            tokio::time::sleep(Duration::from_millis(250)).await;
        }
    }

    /// Durable run events after `after_seq`, until the terminal event.
    pub async fn watch_run(&self, run_id: &str, after_seq: u32) -> Result<impl Stream<Item = Result<Event, Error>>, Error> {
        self.sse(self.request(reqwest::Method::GET, &format!("/api/v1/runs/{run_id}/events?after_seq={after_seq}"),
                              None, "text/event-stream")).await
    }

    pub async fn cancel_run(&self, run_id: &str, reason: &str) -> Result<Run, Error> {
        self.call(reqwest::Method::POST, &format!("/api/v1/runs/{run_id}/cancel"), Some(&json!({"reason": reason})), None).await
    }

    pub async fn resolve_run(&self, run_id: &str, action: &str, note: &str) -> Result<Run, Error> {
        self.call(reqwest::Method::POST, &format!("/api/v1/runs/{run_id}/resolve"),
                  Some(&json!({"action": action, "note": note})), None).await
    }

    /// Preview an agent without executing anything. `request` is a DryRunRequest JSON object.
    pub async fn dry_run(&self, request: Value) -> Result<DryRunReport, Error> {
        self.call(reqwest::Method::POST, "/api/v1/dry-run", Some(&request), None).await
    }
}

fn flush(buf: &mut String) -> Result<Event, Error> {
    let data: Vec<&str> = buf.lines().filter_map(|l| l.strip_prefix("data:")).map(str::trim_start).collect();
    serde_json::from_str::<Event>(&data.join("\n")).map_err(Error::from)
}

fn api_error(status: u16, body: Bytes) -> Error {
    let v: Value = serde_json::from_slice(&body).unwrap_or(Value::Null);
    let e = v.get("error").cloned().unwrap_or_else(|| json!({"code": "http_error",
        "message": String::from_utf8_lossy(&body).chars().take(300).collect::<String>()}));
    let s = |k: &str| e.get(k).and_then(Value::as_str).unwrap_or_default().to_string();
    Error::Api { status, code: s("code"), message: s("message"), request_id: s("request_id"),
                 retryable: e.get("retryable").and_then(Value::as_bool).unwrap_or(false),
                 details: e.get("details").cloned().unwrap_or(Value::Null) }
}
