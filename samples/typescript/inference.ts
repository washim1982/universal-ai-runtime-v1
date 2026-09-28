// UAR inference sample (TypeScript).
//
//   npm install
//   node inference.ts "Explain quantum computing in two sentences"
//
// Sign-in, first match wins:
//   UAR_CLIENT_ID + UAR_CLIENT_SECRET   a registered application: the SDK gets and renews access tokens
//   UAR_API_KEY                         an API key
// Optional: UAR_URL (default http://127.0.0.1:9000), UAR_MODEL (default local:default).
import { UAR, UARError } from "@uar/client";

const url = process.env.UAR_URL ?? "http://127.0.0.1:9000";
const model = process.env.UAR_MODEL ?? "local:default";
const prompt = process.argv.slice(2).join(" ") || "Explain quantum computing in two sentences.";

async function main(): Promise<void> {
  if (!process.env.UAR_CLIENT_ID && !process.env.UAR_API_KEY) {
    console.error("No credentials: set UAR_CLIENT_ID and UAR_CLIENT_SECRET (a registered application) or UAR_API_KEY. See samples/README.md.");
    process.exit(2);
  }
  // The client reads UAR_CLIENT_ID / UAR_CLIENT_SECRET, or UAR_API_KEY, from the environment.
  const client = new UAR(url);
  const who = process.env.UAR_CLIENT_ID ? `application ${process.env.UAR_CLIENT_ID}` : "API key";
  console.log(`UAR ${url} | model ${model} | signed in with ${who}\n`);

  // 1. One request, one complete answer.
  const resp = await client.inference({ model, prompt, maxTokens: 300 });
  console.log(`[${resp.provider}/${resp.model}] ${resp.text}`);
  console.log(`tokens: ${resp.usage?.input_tokens} in, ${resp.usage?.output_tokens} out\n`);

  // 2. The same question, streamed token by token.
  process.stdout.write("streaming: ");
  for await (const event of client.stream({ model, prompt, maxTokens: 300 })) {
    if (event.type === "token") process.stdout.write(event.token?.text ?? "");
    else if (event.type === "error") throw new Error(`stream error: ${event.error?.message}`);
  }
  process.stdout.write("\n");
}

main().catch((e: unknown) => {
  // e.g. 401 wrong credentials, 403 missing permission, 404 unknown model
  if (e instanceof UARError) console.error(`UAR error ${e.status} ${e.code}: ${e.message}`);
  else console.error(e);
  process.exit(1);
});
