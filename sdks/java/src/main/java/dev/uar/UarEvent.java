package dev.uar;

import com.fasterxml.jackson.databind.JsonNode;

/** One stream event; getType() is the populated body ("started", "token", "usage", "completed", "error"). */
public final class UarEvent extends Message {
    UarEvent(JsonNode raw) { super(raw); }

    static UarEvent parse(String json) { return new UarEvent(Json.read(json, JsonNode.class)); }

    public String getType() { return str("type"); }
    public long getSeq() { return num("seq"); }
    public JsonNode getBody() { return node(getType()); }

    /** Token text for "token" events, else null. */
    public String getToken() { return "token".equals(getType()) ? getBody().path("text").asText() : null; }
}
