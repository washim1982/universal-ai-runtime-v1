package dev.uar;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.sun.net.httpserver.HttpServer;

import java.io.IOException;
import java.net.InetSocketAddress;
import java.net.ServerSocket;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.stream.Collectors;
import java.util.stream.Stream;

import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;

/**
 * Conformance: the shared fixtures in contracts/fixtures through this SDK, against uar-mock (default)
 * or a live runtime (UAR_LIVE_URL + UAR_LIVE_KEY; the runtime must use the test configuration).
 */
class ConformanceTest {
    static final Path ROOT = Path.of(System.getenv().getOrDefault("UAR_REPO_ROOT", "../..")).toAbsolutePath().normalize();
    static final Map<String, JsonNode> FIXTURES = new HashMap<>();
    static String url;
    static String key;
    static Process mock;

    @BeforeAll
    static void start() throws Exception {
        try (Stream<Path> files = Files.list(ROOT.resolve("contracts/fixtures"))) {
            for (Path f : files.filter(p -> p.toString().endsWith(".json")).collect(Collectors.toList())) {
                JsonNode fx = Json.read(Files.readString(f), JsonNode.class);
                FIXTURES.put(fx.get("name").asText(), fx);
            }
        }
        String live = System.getenv("UAR_LIVE_URL");
        if (live != null && !live.isEmpty()) {
            url = live;
            key = System.getenv("UAR_LIVE_KEY");
            return;
        }
        int port;
        try (ServerSocket s = new ServerSocket(0)) { port = s.getLocalPort(); }
        boolean win = System.getProperty("os.name").toLowerCase().contains("win");
        Path venv = ROOT.resolve(win ? ".venv/Scripts/python.exe" : ".venv/bin/python");
        String py = System.getenv().getOrDefault("UAR_PYTHON", Files.exists(venv) ? venv.toString() : "python");
        mock = new ProcessBuilder(py, ROOT.resolve("mock/uar_mock.py").toString(), "--port", String.valueOf(port))
                .redirectErrorStream(true).redirectOutput(ProcessBuilder.Redirect.DISCARD).start();
        url = "http://127.0.0.1:" + port;
        key = "uar_mock0000_notasecretjustamockkey";
        HttpClient probe = HttpClient.newHttpClient();
        for (int i = 0; i < 100; i++) {
            try {
                probe.send(HttpRequest.newBuilder(URI.create(url + "/")).build(), HttpResponse.BodyHandlers.discarding());
                break;
            } catch (IOException e) {
                Thread.sleep(100);
            }
        }
    }

    @AfterAll
    static void stop() { if (mock != null) mock.destroyForcibly(); }

    static UARClient client() { return new UARClient(url, key); }

    static JsonNode stripped(JsonNode n, JsonNode fx) {
        JsonNode copy = n.deepCopy();
        for (JsonNode ig : fx.get("response").get("ignore")) {
            String[] parts = ig.asText().split("\\.");
            JsonNode cur = copy;
            for (int i = 0; i < parts.length - 1 && cur != null; i++) cur = cur.get(parts[i]);
            if (cur instanceof ObjectNode o) o.remove(parts[parts.length - 1]);
        }
        return copy;
    }

    static void expect(String name, Message got) {
        JsonNode fx = FIXTURES.get(name);
        assertEquals(stripped(fx.get("response").get("body"), fx), stripped(got.raw(), fx), name);
    }

    static void expectError(Runnable call, int status, String code) {
        UARException e = assertThrows(UARException.class, call::run);
        assertEquals(status, e.getStatus());
        assertEquals(code, e.getCode());
    }

    @Test
    void everyFixtureHasATest() {
        assertEquals(Set.of("inference_basic", "inference_stream", "error_unauthenticated", "tool_execute_read",
                "tool_policy_denied", "run_not_found", "run_start", "dry_run_static"), FIXTURES.keySet());
    }

    @Test
    void inferenceBasic() {
        InferenceResponse r = client().inference("local:default", "hello");
        assertEquals("echo: hello", r.getText());
        assertEquals("fake", r.getProvider());
        expect("inference_basic", r);
    }

    @Test
    void inferenceStream() {
        JsonNode fx = FIXTURES.get("inference_stream");
        List<UarEvent> events;
        try (Stream<UarEvent> s = client().stream("local:default", "stream")) {
            events = s.collect(Collectors.toList());
        }
        JsonNode want = fx.get("response").get("events");
        assertEquals(want.size(), events.size());
        assertEquals("echo: stream", events.stream().map(UarEvent::getToken).filter(t -> t != null).collect(Collectors.joining()));
        for (int i = 0; i < events.size(); i++) {
            ObjectNode raw = (ObjectNode) events.get(i).raw().deepCopy();
            raw.remove("type");
            assertEquals(stripped(want.get(i), fx), stripped(raw, fx), "event " + i);
        }
        assertEquals("completed", events.get(events.size() - 1).getType());
    }

    @Test
    void errorUnauthenticated() {
        expectError(() -> new UARClient(url, "").inference("local:default", "hi"), 401, "unauthenticated");
    }

    @Test
    void toolExecuteRead() {
        ToolResult r = client().executeTool("fs.read_text", Map.of("path", "docs/faq.md"));
        assertTrue(r.isUntrusted());
        expect("tool_execute_read", r);
    }

    @Test
    void toolPolicyDenied() {
        expectError(() -> client().executeTool("fs.write_text", Map.of("path", "docs/x.md", "content", "x")), 403, "policy_denied");
    }

    @Test
    void runNotFound() {
        expectError(() -> client().getRun("run_does_not_exist"), 404, "not_found");
    }

    @Test
    void runStart() {
        Run r = client().runAgent("in_app_assistant", Map.of("prompt", "What is the return window?"));
        assertTrue(r.getRunId().startsWith("run_"));
        expect("run_start", r);
    }

    @Test
    void dryRunStatic() {
        DryRunReport r = client().dryRun(Map.of("agent_id", "in_app_assistant", "mode", "static"));
        assertTrue(r.isExecutedNothing());
        expect("dry_run_static", r);
    }

    @Test
    void noRetryWithoutIdempotencyKey() throws IOException {
        AtomicInteger calls = new AtomicInteger();
        HttpServer server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/", ex -> {
            calls.incrementAndGet();
            byte[] b = "{\"error\":{\"code\":\"unavailable\",\"message\":\"x\",\"retryable\":true}}".getBytes(StandardCharsets.UTF_8);
            ex.sendResponseHeaders(503, b.length);
            ex.getResponseBody().write(b);
            ex.close();
        });
        server.start();
        try {
            UARClient c = new UARClient("http://127.0.0.1:" + server.getAddress().getPort(), "k").withMaxRetries(3);
            expectError(() -> c.inference("m", "p"), 503, "unavailable");
            assertEquals(1, calls.get(), "inference must not be retried");
            calls.set(0);
            assertThrows(UARException.class, () -> c.executeTool("t", Map.of(), "k1"));
            assertEquals(4, calls.get());
        } finally {
            server.stop(0);
        }
    }
}
