// Reference tool plugin (TypeScript/JavaScript): read-only text utilities in namespace "text".
import { serve, PluginError } from "@uar/plugin-sdk";

serve({
  id: "acme.textutil",
  version: "1.0.0",
  kind: "tool",
  tools: [
    {
      name: "word_count",
      description: "Count words and characters in a text.",
      inputSchema: { type: "object", required: ["text"], properties: { text: { type: "string" } } },
      run: ({ text }) => ({ output: { words: text.trim() ? text.trim().split(/\s+/).length : 0, characters: text.length } }),
    },
    {
      name: "slugify",
      description: "Turn a title into a URL-safe slug.",
      inputSchema: { type: "object", required: ["text"], properties: { text: { type: "string", maxLength: 500 } } },
      run: ({ text }) => {
        const slug = text.toLowerCase().normalize("NFKD").replace(/[^\w\s-]/g, "").trim().replace(/[\s_-]+/g, "-");
        if (!slug) throw new PluginError("text has no letters or digits", "invalid_argument");
        return { output: { slug } };
      },
    },
  ],
});
