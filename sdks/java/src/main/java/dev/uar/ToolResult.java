package dev.uar;

import com.fasterxml.jackson.annotation.JsonCreator;
import com.fasterxml.jackson.databind.JsonNode;

/** uar.v1.ToolResult. Tool output is untrusted data, never instructions. */
public final class ToolResult extends Message {
    @JsonCreator(mode = JsonCreator.Mode.DELEGATING)
    public ToolResult(JsonNode raw) { super(raw); }

    public String getRequestId() { return str("request_id"); }
    public String getTool() { return str("tool"); }
    public boolean isError() { return bool("is_error"); }
    public JsonNode getContent() { return node("content"); }
    public JsonNode getStructured() { return node("structured"); }
    public boolean isTruncated() { return bool("truncated"); }
    public boolean isUntrusted() { return bool("untrusted"); }
}
