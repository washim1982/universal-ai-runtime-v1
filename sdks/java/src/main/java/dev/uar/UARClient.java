package dev.uar;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;

import java.io.IOException;
import java.io.UncheckedIOException;
import java.net.URI;
import java.net.URLEncoder;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.ArrayList;
import java.util.Iterator;
import java.util.List;
import java.util.Map;
import java.util.NoSuchElementException;
import java.util.Spliterators;
import java.util.concurrent.ThreadLocalRandom;
import java.util.stream.Stream;
import java.util.stream.StreamSupport;

/**
 * Java client for the Universal AI Runtime (HTTP/JSON + SSE).
 *
 * <pre>{@code
 * UARClient client = new UARClient("http://localhost:9000");          // key from UAR_API_KEY
 * InferenceResponse resp = client.inference("llama3:8b", "Explain quantum computing", "research_agent");
 * System.out.println(resp.getText());
 * }</pre>
 *
 * <p>Only GET requests and requests with an idempotency key are retried (429, 503, network errors);
 * inference and tool calls without a key never are.
 */
public final class UARClient {
    public static final String VERSION = "0.8.0";

    private final String baseUrl;
    private final HttpClient http;
    private String apiKey;
    private String token;
    private int maxRetries = 2;

    public UARClient(String baseUrl) {
        this(baseUrl, System.getenv("UAR_API_KEY"));
    }

    public UARClient(String baseUrl, String apiKey) {
        this.baseUrl = baseUrl.replaceAll("/+$", "");
        this.apiKey = apiKey;
        // HTTP/1.1: the JDK client's cleartext HTTP/2 upgrade (h2c) drops request bodies on some servers.
        this.http = HttpClient.newBuilder().version(HttpClient.Version.HTTP_1_1)
                .connectTimeout(Duration.ofSeconds(10)).build();
    }

    public UARClient withToken(String bearer) { this.token = bearer; return this; }
    public UARClient withMaxRetries(int n) { this.maxRetries = n; return this; }

    private HttpRequest.Builder request(String path, String idem, String accept) {
        HttpRequest.Builder b = HttpRequest.newBuilder(URI.create(baseUrl + path))
                .header("Accept", accept).header("User-Agent", "uar-java/" + VERSION)
                .header("Content-Type", "application/json");
        if (apiKey != null && !apiKey.isEmpty()) b.header("X-API-Key", apiKey);
        else if (token != null) b.header("Authorization", "Bearer " + token);
        if (idem != null) b.header("Idempotency-Key", idem);
        return b;
    }

    private <T> T call(String method, String path, Object body, String idem, Class<T> type) {
        boolean retryable = method.equals("GET") || idem != null;
        String payload = body == null ? null : Json.write(body);
        for (int attempt = 0; ; attempt++) {
            HttpRequest.Builder rb = request(path, idem, "application/json");
            rb.method(method, payload == null ? HttpRequest.BodyPublishers.noBody()
                    : HttpRequest.BodyPublishers.ofString(payload));
            HttpResponse<String> resp = null;
            IOException failure = null;
            try {
                resp = http.send(rb.build(), HttpResponse.BodyHandlers.ofString());
            } catch (IOException e) {
                failure = e;
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                throw new UARException(0, "cancelled", "interrupted", "", false, null);
            }
            if (resp != null && resp.statusCode() < 400) return Json.read(resp.body(), type);
            int status = resp == null ? 0 : resp.statusCode();
            boolean again = retryable && attempt < maxRetries && (failure != null || status == 429 || status == 503);
            if (!again) {
                if (failure != null) throw new UncheckedIOException(failure);
                throw UARException.from(status, resp.body());
            }
            long waitMs = 250L << attempt;
            if (resp != null) {
                String ra = resp.headers().firstValue("Retry-After").orElse("");
                if (ra.matches("\\d+")) waitMs = Math.min(Long.parseLong(ra), 30) * 1000;
            }
            try {
                Thread.sleep((long) (waitMs * (0.5 + ThreadLocalRandom.current().nextDouble())));
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                throw new UARException(0, "cancelled", "interrupted", "", false, null);
            }
        }
    }

    // ------------------------------------------------------------------ inference

    /** Synchronous inference. With a non-null agent, runs that agent on the prompt and returns its result. */
    public InferenceResponse inference(String model, String prompt, String agent) {
        return inference(model, prompt, InferenceOptions.builder().agent(agent).build());
    }

    public InferenceResponse inference(String model, String prompt) {
        return inference(model, prompt, (String) null);
    }

    public InferenceResponse inference(String model, String prompt, InferenceOptions options) {
        return call("POST", "/api/v1/inference", options.toBody(model, prompt, false), null, InferenceResponse.class);
    }

    /**
     * Streaming inference: started, token..., usage, then exactly one completed | error event.
     * Close the stream (try-with-resources) to stop early; that closes the connection and stops generation.
     */
    public Stream<UarEvent> stream(String model, String prompt, InferenceOptions options) {
        HttpRequest req = request("/api/v1/inference", null, "text/event-stream")
                .POST(HttpRequest.BodyPublishers.ofString(Json.write(options.toBody(model, prompt, true)))).build();
        return sse(req);
    }

    public Stream<UarEvent> stream(String model, String prompt) {
        return stream(model, prompt, InferenceOptions.builder().build());
    }

    private Stream<UarEvent> sse(HttpRequest req) {
        HttpResponse<Stream<String>> resp;
        try {
            resp = http.send(req, HttpResponse.BodyHandlers.ofLines());
        } catch (IOException e) {
            throw new UncheckedIOException(e);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new UARException(0, "cancelled", "interrupted", "", false, null);
        }
        if (resp.statusCode() >= 400) {
            String body;
            try (Stream<String> lines = resp.body()) { body = String.join("\n", (Iterable<String>) lines::iterator); }
            throw UARException.from(resp.statusCode(), body);
        }
        Stream<String> lines = resp.body();
        Iterator<String> it = lines.iterator();
        Iterator<UarEvent> events = new Iterator<>() {
            UarEvent next;

            private UarEvent read() {
                StringBuilder data = new StringBuilder();
                while (it.hasNext()) {
                    String line = it.next();
                    if (line.isEmpty()) {
                        if (data.length() > 0) return UarEvent.parse(data.toString());
                    } else if (line.startsWith("data:")) {
                        if (data.length() > 0) data.append('\n');
                        data.append(line.substring(5).stripLeading());
                    }
                }
                return data.length() > 0 ? UarEvent.parse(data.toString()) : null;
            }

            public boolean hasNext() { if (next == null) next = read(); return next != null; }

            public UarEvent next() {
                if (!hasNext()) throw new NoSuchElementException();
                UarEvent e = next;
                next = null;
                return e;
            }
        };
        return StreamSupport.stream(Spliterators.spliteratorUnknownSize(events, 0), false).onClose(lines::close);
    }

    // ------------------------------------------------------------------ tools & catalog

    /** Run a governed tool. Pass an idempotency key (or null) to make retries safe. */
    public ToolResult executeTool(String tool, Map<String, ?> args, String idempotencyKey) {
        return call("POST", "/api/v1/tool/execute", Map.of("tool", tool, "args", args), idempotencyKey, ToolResult.class);
    }

    public ToolResult executeTool(String tool, Map<String, ?> args) { return executeTool(tool, args, null); }

    public List<JsonNode> listModels() { return items(call("GET", "/api/v1/models", null, null, JsonNode.class), "models"); }

    public List<JsonNode> listTools() { return items(call("GET", "/api/v1/tools", null, null, JsonNode.class), "tools"); }

    private static List<JsonNode> items(JsonNode n, String field) {
        List<JsonNode> out = new ArrayList<>();
        if (n.get(field) instanceof ArrayNode a) a.forEach(out::add);
        return out;
    }

    // ------------------------------------------------------------------ agents & runs

    public AgentVersion registerAgent(Map<String, ?> definition) {
        return call("POST", "/api/v1/agents", Map.of("definition", definition), null, AgentVersion.class);
    }

    /** Start a run (202). Pass an idempotency key (or null) to make it safe to retry. */
    public Run runAgent(String agentId, Map<String, ?> input, String idempotencyKey) {
        return call("POST", "/api/v1/agent/run", Map.of("agent_id", agentId, "input", input == null ? Map.of() : input),
                idempotencyKey, Run.class);
    }

    public Run runAgent(String agentId, Map<String, ?> input) { return runAgent(agentId, input, null); }

    public Run getRun(String runId) { return call("GET", "/api/v1/runs/" + enc(runId), null, null, Run.class); }

    /** Poll until the run is terminal or needs attention. */
    public Run waitRun(String runId, Duration timeout) {
        long deadline = System.nanoTime() + timeout.toNanos();
        while (true) {
            Run r = getRun(runId);
            if (List.of("succeeded", "failed", "cancelled", "needs_attention").contains(r.getStatus())) return r;
            if (System.nanoTime() > deadline) throw new UARException(0, "deadline_exceeded", "run " + runId + " still " + r.getStatus(), "", true, null);
            try { Thread.sleep(250); } catch (InterruptedException e) { Thread.currentThread().interrupt(); return r; }
        }
    }

    /** Durable run events after afterSeq, until the terminal event. Close the stream when done. */
    public Stream<UarEvent> watchRun(String runId, int afterSeq) {
        return sse(request("/api/v1/runs/" + enc(runId) + "/events?after_seq=" + afterSeq, null, "text/event-stream")
                .GET().build());
    }

    public Run cancelRun(String runId, String reason) {
        return call("POST", "/api/v1/runs/" + enc(runId) + "/cancel", Map.of("reason", reason == null ? "" : reason), null, Run.class);
    }

    public Run resolveRun(String runId, String action, String note) {
        return call("POST", "/api/v1/runs/" + enc(runId) + "/resolve", Map.of("action", action, "note", note == null ? "" : note),
                null, Run.class);
    }

    /** Preview an agent without executing anything. request: a DryRunRequest (agent_id | definition | inference, mode, ...). */
    public DryRunReport dryRun(Map<String, ?> request) {
        return call("POST", "/api/v1/dry-run", request, null, DryRunReport.class);
    }

    private static String enc(String s) { return URLEncoder.encode(s, StandardCharsets.UTF_8); }

    /** Build an ObjectNode (for DryRun requests and agent definitions from JSON text). */
    public static ObjectNode json(String text) { return (ObjectNode) Json.read(text, JsonNode.class); }
}
