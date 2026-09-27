package dev.uar;

import com.fasterxml.jackson.annotation.JsonValue;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.node.JsonNodeFactory;

/** Base for response types: typed getters over the raw JSON, which round-trips exactly. */
public abstract class Message {
    protected final JsonNode raw;

    protected Message(JsonNode raw) {
        this.raw = raw == null ? JsonNodeFactory.instance.objectNode() : raw;
    }

    /** The complete JSON document, including fields newer than this SDK. */
    @JsonValue
    public JsonNode raw() { return raw; }

    protected String str(String f) {
        JsonNode n = raw.get(f);
        return n == null || n.isNull() ? "" : n.asText();
    }

    protected boolean bool(String f) {
        JsonNode n = raw.get(f);
        return n != null && n.asBoolean();
    }

    protected long num(String f) {
        JsonNode n = raw.get(f);
        return n == null ? 0 : n.asLong();
    }

    protected JsonNode node(String f) { return raw.get(f); }

    @Override
    public String toString() { return getClass().getSimpleName() + raw; }
}
