package dev.uar;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/** Optional inference settings: {@code InferenceOptions.builder().agent("x").maxTokens(200).build()}. */
public final class InferenceOptions {
    private final Map<String, Object> body = new LinkedHashMap<>();
    private final Map<String, Object> params = new LinkedHashMap<>();
    private List<?> messages;

    public static Builder builder() { return new Builder(); }

    Map<String, Object> toBody(String model, String prompt, boolean stream) {
        Map<String, Object> b = new LinkedHashMap<>();
        b.put("model", model);
        if (messages != null && !messages.isEmpty()) {
            b.put("messages", messages);
        } else {
            b.put("input", prompt);
        }
        b.putAll(body);
        if (!params.isEmpty()) b.put("params", params);
        if (stream) b.put("stream", true);
        return b;
    }

    public static final class Builder {
        private final InferenceOptions o = new InferenceOptions();

        public Builder messages(List<Map<String, ?>> m) { o.messages = m; return this; }
        public Builder agent(String agent) { if (agent != null) o.body.put("agent", agent); return this; }
        public Builder tools(List<String> tools) { o.body.put("tools", tools); return this; }
        public Builder toolMode(String mode) { o.body.put("tool_mode", mode); return this; }
        public Builder dataClass(String dataClass) { o.body.put("data_class", dataClass); return this; }
        public Builder extensions(Map<String, ?> extensions) { o.body.put("extensions", extensions); return this; }
        public Builder temperature(double t) { o.params.put("temperature", t); return this; }
        public Builder maxTokens(int n) { o.params.put("max_tokens", n); return this; }
        public Builder responseSchema(Map<String, ?> schema) { o.params.put("response_schema", schema); return this; }
        public InferenceOptions build() { return o; }
    }
}
