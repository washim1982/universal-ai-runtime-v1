package dev.uar;

import com.fasterxml.jackson.annotation.JsonCreator;
import com.fasterxml.jackson.databind.JsonNode;

/** uar.v1.InferenceResponse. */
public final class InferenceResponse extends Message {
    @JsonCreator(mode = JsonCreator.Mode.DELEGATING)
    public InferenceResponse(JsonNode raw) { super(raw); }

    public String getRequestId() { return str("request_id"); }
    public String getProvider() { return str("provider"); }
    public String getModel() { return str("model"); }
    public String getContent() { return str("content"); }
    /** The generated text (alias of getContent). */
    public String getText() { return str("content"); }
    public String getFinishReason() { return str("finish_reason"); }
    public String getRunId() { return str("run_id"); }
    public JsonNode getToolCalls() { return node("tool_calls"); }
    public JsonNode getUsage() { return node("usage"); }
    public JsonNode getRoute() { return node("route"); }
    public JsonNode getOutput() { return node("output"); }
}
