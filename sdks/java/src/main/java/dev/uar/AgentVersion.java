package dev.uar;

import com.fasterxml.jackson.annotation.JsonCreator;
import com.fasterxml.jackson.databind.JsonNode;

/** uar.v1.AgentVersion. */
public final class AgentVersion extends Message {
    @JsonCreator(mode = JsonCreator.Mode.DELEGATING)
    public AgentVersion(JsonNode raw) { super(raw); }

    public String getAgentId() { return str("agent_id"); }
    public String getVersion() { return str("version"); }
    public String getDigest() { return str("digest"); }
}
