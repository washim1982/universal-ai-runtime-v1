package dev.uar;

import com.fasterxml.jackson.databind.JsonNode;

/** A structured runtime error ({code, message, request_id, retryable, details}). */
public final class UARException extends RuntimeException {
    private final int status;
    private final String code;
    private final String requestId;
    private final boolean retryable;
    private final transient JsonNode details;

    public UARException(int status, String code, String message, String requestId, boolean retryable, JsonNode details) {
        super(code + ": " + message + " (status " + status + ", request " + requestId + ")");
        this.status = status;
        this.code = code;
        this.requestId = requestId;
        this.retryable = retryable;
        this.details = details;
    }

    static UARException from(int status, String body) {
        JsonNode err = null;
        try {
            err = Json.read(body, JsonNode.class).get("error");
        } catch (RuntimeException ignored) {
            // not JSON: reported as http_error below
        }
        if (err == null) {
            return new UARException(status, "http_error", body.length() > 300 ? body.substring(0, 300) : body, "", false, null);
        }
        return new UARException(status, err.path("code").asText(), err.path("message").asText(),
                err.path("request_id").asText(), err.path("retryable").asBoolean(), err.get("details"));
    }

    public int getStatus() { return status; }
    public String getCode() { return code; }
    public String getRequestId() { return requestId; }
    public boolean isRetryable() { return retryable; }
    public JsonNode getDetails() { return details; }
    public boolean isAuthentication() { return status == 401; }
    public boolean isPermissionDenied() { return status == 403; }
    public boolean isNotFound() { return status == 404; }
    public boolean isRateLimited() { return status == 429; }
}
