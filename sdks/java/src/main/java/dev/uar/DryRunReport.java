package dev.uar;

import com.fasterxml.jackson.annotation.JsonCreator;
import com.fasterxml.jackson.databind.JsonNode;

/** uar.v1.DryRunReport. Nothing was executed to produce it. */
public final class DryRunReport extends Message {
    @JsonCreator(mode = JsonCreator.Mode.DELEGATING)
    public DryRunReport(JsonNode raw) { super(raw); }

    public String getMode() { return str("mode"); }
    public boolean isValid() { return bool("valid"); }
    public JsonNode getErrors() { return node("errors"); }
    public JsonNode getSteps() { return node("steps"); }
    public JsonNode getBranches() { return node("branches"); }
    public JsonNode getUnresolved() { return node("unresolved"); }
    public boolean isExecutedNothing() { return bool("executed_nothing"); }
}
