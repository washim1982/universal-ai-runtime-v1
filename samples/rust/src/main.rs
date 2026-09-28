//! UAR inference sample (Rust).
//!
//!     cargo run -- "Explain quantum computing in two sentences"
//!
//! Sign-in, first match wins:
//!   UAR_CLIENT_ID + UAR_CLIENT_SECRET   a registered application: exchanged for an access token
//!   UAR_API_KEY                         an API key
//! Optional: UAR_URL (default http://127.0.0.1:9000), UAR_MODEL (default local:default).
use std::{env, error::Error, io::Write};

use futures_util::StreamExt;
use uar::{Client, InferenceOptions};

/// Exchange an application's client credentials at the token service (OAuth 2.0 client_credentials).
async fn access_token(base: &str, id: &str, secret: &str) -> Result<String, Box<dyn Error>> {
    let resp = reqwest::Client::new()
        .post(format!("{base}/api/v1/oauth/token"))
        .form(&[("grant_type", "client_credentials"), ("client_id", id), ("client_secret", secret)])
        .send().await?;
    let status = resp.status();
    let body: serde_json::Value = resp.json().await?;
    match body["access_token"].as_str() {
        Some(t) => Ok(t.to_string()),
        None => Err(format!("token request failed ({status}): {} {}", body["error"], body["error_description"]).into()),
    }
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn Error>> {
    let base = env::var("UAR_URL").unwrap_or_else(|_| "http://127.0.0.1:9000".into());
    let model = env::var("UAR_MODEL").unwrap_or_else(|_| "local:default".into());
    let args: Vec<String> = env::args().skip(1).collect();
    let prompt = if args.is_empty() { "Explain quantum computing in two sentences.".to_string() } else { args.join(" ") };

    if env::var("UAR_CLIENT_ID").unwrap_or_default().is_empty() && env::var("UAR_API_KEY").unwrap_or_default().is_empty() {
        eprintln!("No credentials: set UAR_CLIENT_ID and UAR_CLIENT_SECRET (a registered application) or UAR_API_KEY. See samples/README.md.");
        std::process::exit(2);
    }
    let mut client = Client::new(&base); // picks up UAR_API_KEY
    let mut who = "API key".to_string();
    if let Ok(id) = env::var("UAR_CLIENT_ID") {
        let token = access_token(&base, &id, &env::var("UAR_CLIENT_SECRET").unwrap_or_default()).await?;
        client = client.with_api_key("").with_token(token);
        who = format!("application {id}");
    }
    println!("UAR {base} | model {model} | signed in with {who}\n");
    let opts = InferenceOptions { max_tokens: Some(300), ..Default::default() };

    // 1. One request, one complete answer. Errors are uar::Error (e.g. 401, 403, 404 with a code).
    let resp = client.inference_with(&model, &prompt, &opts).await?;
    println!("[{}/{}] {}", resp.provider, resp.model, resp.text());
    let usage = resp.usage.unwrap_or_default();
    println!("tokens: {} in, {} out\n", usage["input_tokens"], usage["output_tokens"]);

    // 2. The same question, streamed token by token.
    print!("streaming: ");
    let mut stream = Box::pin(client.stream(&model, &prompt, &opts).await?);
    while let Some(event) = stream.next().await {
        let event = event?;
        match event.kind.as_str() {
            "token" => { print!("{}", event.token().unwrap_or_default()); std::io::stdout().flush()?; }
            "error" => return Err(format!("stream error: {}", event.body().map(|b| b["message"].to_string()).unwrap_or_default()).into()),
            _ => {}
        }
    }
    println!();
    Ok(())
}
