"""UAR inference sample (Python).

    pip install -r requirements.txt
    python inference.py "Explain quantum computing in two sentences"

Sign-in, first match wins:
  UAR_CLIENT_ID + UAR_CLIENT_SECRET   a registered application: the SDK gets and renews access tokens
  UAR_API_KEY                         an API key
Optional: UAR_URL (default http://127.0.0.1:9000), UAR_MODEL (default local:default).
"""
import os
import sys
#uar_2db15411cc5a_IIW6B-FT6UT5S7tuJY1HaPvxzSFBTTtMv-m4SMxr2F8
#python-sample-cd250192
#uars_7081XevU4ZZrWeRElVnOifM7fnLDBAuxquc75swuHZbY2QJHMLGsYg
#http://127.0.0.1:9000/api/v1/oauth/token
from uar import Client, UARError

URL = os.environ.get("UAR_URL", "http://127.0.0.1:9000")
MODEL = os.environ.get("UAR_MODEL", "local:default")
PROMPT = " ".join(sys.argv[1:]) or "Explain quantum computing in two sentences."


def main() -> int:
    if not (os.environ.get("UAR_CLIENT_ID") or os.environ.get("UAR_API_KEY")):
        sys.exit("No credentials: set UAR_CLIENT_ID and UAR_CLIENT_SECRET (a registered application) or UAR_API_KEY. See samples/README.md.")
    # Client() reads UAR_CLIENT_ID / UAR_CLIENT_SECRET, or UAR_API_KEY, from the environment.
    with Client(URL) as client:
        who = "application " + client.client_id if client.client_id else "API key"
        print(f"UAR {URL} | model {MODEL} | signed in with {who}\n")

        # 1. One request, one complete answer.
        resp = client.inference(model=MODEL, prompt=PROMPT, max_tokens=300)
        print(f"[{resp.provider}/{resp.model}] {resp.text}")
        usage = resp.get("usage", {})
        print(f"tokens: {usage.get('input_tokens')} in, {usage.get('output_tokens')} out\n")

        # 2. The same question, streamed token by token.
        print("streaming: ", end="", flush=True)
        for event in client.stream(model=MODEL, prompt=PROMPT, max_tokens=300):
            if event.type == "token":
                print(event.body["text"], end="", flush=True)
            elif event.type == "error":
                print(f"\nstream error: {event.body['message']}")
                return 1
        print()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except UARError as e:   # e.g. 401 wrong credentials, 403 missing permission, 404 unknown model
        sys.exit(f"UAR error {e.status} {e.code}: {e.message}")
