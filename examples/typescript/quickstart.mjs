// The original UAR example, runnable against a local runtime.
//   set UAR_API_KEY=<developer key from .local/credentials.env>
//   node examples/typescript/quickstart.mjs          (after: cd sdks/typescript && npm install && npm run build)
import { UAR } from "../../sdks/typescript/dist/index.js";

const client = new UAR("http://localhost:9000");

const resp = await client.inference({
  model: "local:default",
  prompt: "Explain quantum computing in two sentences.",
});
console.log(resp.text);

const answer = await client.inference({
  model: "local:default",
  prompt: "How long do I have to return a chair?",
  agent: "in_app_assistant",
});
console.log(answer.text, `(run ${answer.run_id})`);

for await (const ev of client.stream({ model: "local:default", prompt: "Name three planets." })) {
  if (ev.type === "token") process.stdout.write(ev.token.text);
}
console.log();
