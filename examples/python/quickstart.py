"""The original UAR example, runnable against a local runtime.

    set UAR_API_KEY=<developer key from .local/credentials.env>
    python examples/python/quickstart.py
"""
from uar import Client

client = Client("http://localhost:9000")

resp = client.inference(
    model="local:default",
    prompt="Explain quantum computing in two sentences.",
)
print(resp.text)
print(f"-- {resp.provider}/{resp.model}, {resp.usage.output_tokens} output tokens")

# The same call routed through an agent (the in-app assistant answers from the product FAQ).
resp = client.inference(model="local:default", prompt="How long do I have to return a chair?",
                        agent="in_app_assistant")
print(resp.text)
print(f"-- agent run {resp.run_id}")

# Streaming.
for event in client.stream(model="local:default", prompt="Name three planets."):
    if event.type == "token":
        print(event.body["text"], end="", flush=True)
print()

# Offline dry-run of the report agent: nothing is executed.
plan = client.dry_run("report_generator")
print("dry-run valid:", plan.valid, "| steps:", [s["node_id"] for s in plan.steps])
