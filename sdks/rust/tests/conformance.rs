//! Conformance: the shared fixtures in contracts/fixtures through this SDK, against uar-mock (default)
//! or a live runtime (UAR_LIVE_URL + UAR_LIVE_KEY; the runtime must use the test configuration).

use std::collections::BTreeMap;
use std::net::TcpListener;
use std::path::PathBuf;
use std::process::{Child, Command};
use std::time::Duration;

use futures_util::StreamExt;
use serde_json::{json, Value};

fn root() -> PathBuf { PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("..").join("..") }

fn fixtures() -> BTreeMap<String, Value> {
    let mut out = BTreeMap::new();
    for e in std::fs::read_dir(root().join("contracts").join("fixtures")).unwrap() {
        let p = e.unwrap().path();
        if p.extension().map(|x| x == "json").unwrap_or(false) {
            let v: Value = serde_json::from_str(&std::fs::read_to_string(&p).unwrap()).unwrap();
            out.insert(v["name"].as_str().unwrap().to_string(), v);
        }
    }
    out
}

struct Target { url: String, key: String, mock: Option<Child> }
impl Drop for Target { fn drop(&mut self) { if let Some(m) = &mut self.mock { let _ = m.kill(); } } }

async fn target() -> Target {
    if let Ok(url) = std::env::var("UAR_LIVE_URL") {
        return Target { url, key: std::env::var("UAR_LIVE_KEY").unwrap_or_default(), mock: None };
    }
    let port = TcpListener::bind("127.0.0.1:0").unwrap().local_addr().unwrap().port();
    let venv = if cfg!(windows) { root().join(".venv/Scripts/python.exe") } else { root().join(".venv/bin/python") };
    let py = std::env::var("UAR_PYTHON").map(PathBuf::from)
        .unwrap_or_else(|_| if venv.exists() { venv } else { PathBuf::from("python") });
    let child = Command::new(py).arg(root().join("mock/uar_mock.py")).args(["--port", &port.to_string()])
        .spawn().expect("start uar-mock");
    let url = format!("http://127.0.0.1:{port}");
    for _ in 0..100 {
        if reqwest::get(format!("{url}/")).await.is_ok() { break; }
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    Target { url, key: "uar_mock0000_notasecretjustamockkey".into(), mock: Some(child) }
}

fn drop_ignored(v: &mut Value, ignore: &[String]) {
    for path in ignore {
        let (parent, last) = match path.rsplit_once('.') {
            Some((p, l)) => (format!("/{}", p.replace('.', "/")), l),
            None => (String::new(), path.as_str()),
        };
        if let Some(m) = v.pointer_mut(&parent).and_then(Value::as_object_mut) { m.remove(last); }
    }
}

/// Keep, at every level, only keys the fixture mentions (typed structs omit defaults, fixtures may list them).
fn project(have: &Value, want: &Value) -> Value {
    match (have, want) {
        (Value::Object(h), Value::Object(w)) => Value::Object(w.iter().filter_map(|(k, wv)| {
            h.get(k).map(|hv| (k.clone(), project(hv, wv)))
        }).collect()),
        (Value::Array(h), Value::Array(w)) if h.len() == w.len() =>
            Value::Array(h.iter().zip(w).map(|(a, b)| project(a, b)).collect()),
        _ => have.clone(),
    }
}

/// Fill defaults the typed struct skipped (false, "", 0, []) so equality is about values, not presence.
fn with_defaults(have: &Value, want: &Value) -> Value {
    match (have, want) {
        (Value::Object(h), Value::Object(w)) => {
            let mut out = h.clone();
            for (k, wv) in w {
                let hv = h.get(k).cloned().unwrap_or_else(|| match wv {
                    Value::Bool(_) => json!(false), Value::String(_) => json!(""),
                    Value::Number(_) => json!(0), Value::Array(_) => json!([]), _ => Value::Null });
                out.insert(k.clone(), with_defaults(&hv, wv));
            }
            Value::Object(out)
        }
        (Value::Array(h), Value::Array(w)) if h.len() == w.len() =>
            Value::Array(h.iter().zip(w).map(|(a, b)| with_defaults(a, b)).collect()),
        _ => have.clone(),
    }
}

fn numbers_as_f64(v: &Value) -> Value {
    match v {
        Value::Number(n) => json!(n.as_f64().unwrap()),
        Value::Array(a) => Value::Array(a.iter().map(numbers_as_f64).collect()),
        Value::Object(o) => Value::Object(o.iter().map(|(k, x)| (k.clone(), numbers_as_f64(x))).collect()),
        _ => v.clone(),
    }
}

fn assert_matches(name: &str, fx: &Value, have: &Value, want: &Value) {
    let ignore: Vec<String> = fx["response"]["ignore"].as_array().unwrap().iter()
        .map(|x| x.as_str().unwrap().to_string()).collect();
    let (mut h, mut w) = (have.clone(), want.clone());
    drop_ignored(&mut h, &ignore);
    drop_ignored(&mut w, &ignore);
    let h = numbers_as_f64(&project(&with_defaults(&h, &w), &w));
    assert_eq!(h, numbers_as_f64(&w), "fixture {name}");
}

fn api(err: uar::Error, status: u16, code: &str) {
    assert_eq!((err.status(), err.code()), (Some(status), Some(code)), "{err}");
}

#[tokio::test]
async fn conformance() {
    let t = target().await;
    let fx = fixtures();
    let c = uar::Client::new(&t.url).with_api_key(t.key.clone());
    let body = |n: &str| fx[n]["response"]["body"].clone();
    let mut done = vec![];

    let r = c.inference_with("local:default", "hello", &Default::default()).await.unwrap();
    assert_eq!(r.text(), "echo: hello");
    assert_matches("inference_basic", &fx["inference_basic"], &serde_json::to_value(&r).unwrap(), &body("inference_basic"));
    done.push("inference_basic");

    let events: Vec<uar::Event> = c.stream("local:default", "stream", &Default::default()).await.unwrap()
        .map(|e| e.unwrap()).collect().await;
    let want = fx["inference_stream"]["response"]["events"].as_array().unwrap();
    assert_eq!(events.len(), want.len());
    assert_eq!(events.iter().filter_map(|e| e.token()).collect::<String>(), "echo: stream");
    for (e, w) in events.iter().zip(want) {
        let mut v = serde_json::to_value(e).unwrap();
        v.as_object_mut().unwrap().remove("type");
        assert_matches("inference_stream", &fx["inference_stream"], &v, w);
    }
    assert_eq!(events.last().unwrap().kind, "completed");
    done.push("inference_stream");

    let anon = uar::Client::new(&t.url).with_api_key("");
    api(anon.inference("local:default", "hi", None).await.unwrap_err(), 401, "unauthenticated");
    done.push("error_unauthenticated");

    let r = c.execute_tool("fs.read_text", json!({"path": "docs/faq.md"}), None).await.unwrap();
    assert_matches("tool_execute_read", &fx["tool_execute_read"], &serde_json::to_value(&r).unwrap(), &body("tool_execute_read"));
    done.push("tool_execute_read");

    api(c.execute_tool("fs.write_text", json!({"path": "docs/x.md", "content": "x"}), None).await.unwrap_err(),
        403, "policy_denied");
    done.push("tool_policy_denied");

    api(c.get_run("run_does_not_exist").await.unwrap_err(), 404, "not_found");
    done.push("run_not_found");

    let r = c.run_agent("in_app_assistant", json!({"prompt": "What is the return window?"}), None).await.unwrap();
    assert_matches("run_start", &fx["run_start"], &serde_json::to_value(&r).unwrap(), &body("run_start"));
    done.push("run_start");

    let r = c.dry_run(json!({"agent_id": "in_app_assistant", "mode": "static"})).await.unwrap();
    assert!(r.executed_nothing);
    assert_matches("dry_run_static", &fx["dry_run_static"], &serde_json::to_value(&r).unwrap(), &body("dry_run_static"));
    done.push("dry_run_static");

    let missing: Vec<&String> = fx.keys().filter(|k| !done.contains(&k.as_str())).collect();
    assert!(missing.is_empty(), "no SDK test for fixtures {missing:?}");
}

#[tokio::test]
async fn no_retry_without_idempotency_key() {
    use std::io::{Read, Write};
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::Arc;
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let addr = listener.local_addr().unwrap();
    let calls = Arc::new(AtomicUsize::new(0));
    let counter = calls.clone();
    std::thread::spawn(move || {
        for stream in listener.incoming() {
            let mut s = stream.unwrap();
            let mut buf = [0u8; 8192];
            let _ = s.read(&mut buf);
            counter.fetch_add(1, Ordering::SeqCst);
            let body = r#"{"error":{"code":"unavailable","message":"x","retryable":true}}"#;
            let _ = write!(s, "HTTP/1.1 503 Service Unavailable\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}", body.len(), body);
        }
    });
    let c = uar::Client::new(&format!("http://{addr}")).with_api_key("k").with_max_retries(3);
    api(c.inference("m", "p", None).await.unwrap_err(), 503, "unavailable");
    assert_eq!(calls.load(Ordering::SeqCst), 1, "inference must not be retried");
    calls.store(0, Ordering::SeqCst);
    let _ = c.execute_tool("t", json!({}), Some("k1")).await;
    assert_eq!(calls.load(Ordering::SeqCst), 4);
}
