// The UAR Rust example (depends on sdks/rust as crate `uar-client`, lib name `uar`).
#[tokio::main]
async fn main() -> Result<(), uar::Error> {
    let client = uar::Client::new("http://localhost:9000"); // key from UAR_API_KEY
    let resp = client.inference("local:default", "Explain quantum computing", Some("in_app_assistant")).await?;
    println!("{}", resp.text());
    Ok(())
}
