package dev.uar;

import com.fasterxml.jackson.annotation.JsonCreator;
import com.fasterxml.jackson.databind.JsonNode;

/** uar.v1.Run. */
public final class Run extends Message {
    @JsonCreator(mode = JsonCreator.Mode.DELEGATING)
    public Run(JsonNode raw) { super(raw); }

    public String getRunId() { return str("run_id"); }
    public String getAgentId() { return str("agent_id"); }
    public String getVersion() { return str("version"); }
    /** queued | running | waiting_approval | succeeded | failed | cancelled | needs_attention */
    public String getStatus() { return str("status"); }
    public long getSteps() { return num("steps"); }
    public String getCurrentNode() { return str("current_node"); }
    public JsonNode getOutput() { return node("output"); }
    public JsonNode getError() { return node("error"); }
    public JsonNode getUsage() { return node("usage"); }
    public boolean isCancelRequested() { return bool("cancel_requested"); }
}
