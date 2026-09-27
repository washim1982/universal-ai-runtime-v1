package dev.uar.plugin;

import com.google.protobuf.InvalidProtocolBufferException;
import com.google.protobuf.Struct;
import com.google.protobuf.util.JsonFormat;

import dev.uar.plugin.v1.AgentCall;
import dev.uar.plugin.v1.DescribeRequest;
import dev.uar.plugin.v1.HealthRequest;
import dev.uar.plugin.v1.HealthResponse;
import dev.uar.plugin.v1.ModelCall;
import dev.uar.plugin.v1.PluginDescriptor;
import dev.uar.plugin.v1.PluginEvent;
import dev.uar.plugin.v1.PluginExecuteRequest;
import dev.uar.plugin.v1.PluginExecuteResponse;
import dev.uar.plugin.v1.PluginGrpc;
import dev.uar.plugin.v1.PluginInitRequest;
import dev.uar.plugin.v1.PluginInitResponse;
import dev.uar.plugin.v1.ShutdownRequest;
import dev.uar.plugin.v1.ShutdownResponse;
import dev.uar.plugin.v1.ToolCall;
import dev.uar.plugin.v1.ToolDescriptor;

import io.grpc.Server;
import io.grpc.netty.shaded.io.grpc.netty.NettyServerBuilder;
import io.grpc.stub.StreamObserver;

import java.io.IOException;
import java.net.InetSocketAddress;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CountDownLatch;

/**
 * Write UAR plugins in Java.
 *
 * <pre>{@code
 * UarPlugin.serve(UarPlugin.definition("acme.wordfreq", "1.0.0", "agent")
 *     .agent((agent, input) -> Reply.output(Map.of("top", List.of("uar"))))
 *     .build());
 * }</pre>
 *
 * <p>The runtime starts the plugin with UAR_PLUGIN_ADDR set and speaks uar.plugin.v1 over gRPC.
 * Maps passed in and out are plain JSON (String, Number, Boolean, List, Map, null).
 */
public final class UarPlugin {
    private UarPlugin() {}

    /** A handler result: text and/or structured output, optionally streamed tokens. */
    public record Reply(String text, Map<String, Object> output, List<String> tokens, String finishReason,
                        int inputTokens, int outputTokens) {
        public static Reply text(String t) { return new Reply(t, null, null, "stop", 0, 0); }
        public static Reply output(Map<String, Object> o) { return new Reply("", o, null, "stop", 0, 0); }
    }

    /** A domain error the caller should see. */
    public static final class PluginException extends RuntimeException {
        private final String code;
        public PluginException(String message, String code) { super(message); this.code = code; }
        public PluginException(String message) { this(message, "plugin_error"); }
        public String code() { return code; }
    }

    @FunctionalInterface public interface ModelHandler { Reply handle(String model, List<Map<String, Object>> messages, Map<String, Object> params) throws Exception; }
    @FunctionalInterface public interface ToolHandler { Reply handle(Map<String, Object> args) throws Exception; }
    @FunctionalInterface public interface AgentHandler { Reply handle(String agent, Map<String, Object> input) throws Exception; }
    @FunctionalInterface public interface InitHandler { void init(Map<String, Object> config, Map<String, String> secrets) throws Exception; }

    public record Tool(String name, String description, Map<String, Object> inputSchema, String sideEffect, ToolHandler run) {}

    public static final class Definition {
        final String id, version, kind;
        final List<String> models = new ArrayList<>(), capabilities = new ArrayList<>();
        final List<Tool> tools = new ArrayList<>();
        ModelHandler model;
        AgentHandler agent;
        InitHandler init;

        Definition(String id, String version, String kind) { this.id = id; this.version = version; this.kind = kind; }

        public Definition models(String... m) { models.addAll(List.of(m)); return this; }
        public Definition capabilities(String... c) { capabilities.addAll(List.of(c)); return this; }
        public Definition tool(String name, String description, Map<String, Object> schema, ToolHandler run) {
            tools.add(new Tool(name, description, schema, "read", run)); return this;
        }
        public Definition model(ModelHandler h) { model = h; return this; }
        public Definition agent(AgentHandler h) { agent = h; return this; }
        public Definition init(InitHandler h) { init = h; return this; }
        public Definition build() { return this; }
    }

    /** Start a definition; kind is model | tool | agent. */
    public static Definition definition(String id, String version, String kind) { return new Definition(id, version, kind); }

    // ------------------------------------------------------------------ JSON <-> Struct

    @SuppressWarnings("unchecked")
    static Map<String, Object> toMap(Struct s) {
        try {
            return new com.google.gson.Gson().fromJson(JsonFormat.printer().print(s), Map.class);
        } catch (InvalidProtocolBufferException e) {
            throw new IllegalStateException(e);
        }
    }

    static Struct toStruct(Map<String, ?> m) {
        Struct.Builder b = Struct.newBuilder();
        if (m == null) return b.build();
        try {
            JsonFormat.parser().merge(new com.google.gson.Gson().toJson(m), b);
        } catch (InvalidProtocolBufferException e) {
            throw new IllegalArgumentException(e);
        }
        return b.build();
    }

    // ------------------------------------------------------------------ server

    static final class Service extends PluginGrpc.PluginImplBase {
        private final Definition d;
        private final CountDownLatch stop;

        Service(Definition d, CountDownLatch stop) { this.d = d; this.stop = stop; }

        @Override
        public void describe(DescribeRequest request, StreamObserver<PluginDescriptor> out) {
            PluginDescriptor.Builder b = PluginDescriptor.newBuilder().setId(d.id).setVersion(d.version).setKind(d.kind)
                    .setApi("uar.plugin.v1").addAllModels(d.models).addAllCapabilities(d.capabilities);
            for (Tool t : d.tools) {
                b.addTools(ToolDescriptor.newBuilder().setName(t.name()).setDescription(t.description())
                        .setInputSchema(toStruct(t.inputSchema())).setSideEffect(t.sideEffect()));
            }
            out.onNext(b.build());
            out.onCompleted();
        }

        @Override
        public void init(PluginInitRequest request, StreamObserver<PluginInitResponse> out) {
            PluginInitResponse.Builder r = PluginInitResponse.newBuilder().setOk(true);
            try {
                if (d.init != null) d.init.init(toMap(request.getConfig()), request.getSecretsMap());
            } catch (Exception e) {
                r.setOk(false).setMessage(String.valueOf(e.getMessage()));
            }
            out.onNext(r.build());
            out.onCompleted();
        }

        private Reply dispatch(PluginExecuteRequest r) throws Exception {
            switch (r.getCallCase()) {
                case MODEL -> {
                    if (d.model == null) throw new PluginException("this plugin does not serve models", "unimplemented");
                    ModelCall m = r.getModel();
                    List<Map<String, Object>> msgs = new ArrayList<>();
                    m.getMessagesList().forEach(s -> msgs.add(toMap(s)));
                    return d.model.handle(m.getModel(), msgs, toMap(m.getParams()));
                }
                case TOOL -> {
                    ToolCall t = r.getTool();
                    for (Tool tool : d.tools) if (tool.name().equals(t.getTool())) return tool.run().handle(toMap(t.getArgs()));
                    throw new PluginException("unknown tool " + t.getTool(), "not_found");
                }
                case AGENT -> {
                    if (d.agent == null) throw new PluginException("this plugin does not run agents", "unimplemented");
                    AgentCall a = r.getAgent();
                    return d.agent.handle(a.getAgent(), toMap(a.getInput()));
                }
                default -> throw new PluginException("empty request", "invalid_argument");
            }
        }

        private static PluginExecuteResponse fin(Reply r) {
            String text = r.text() != null && !r.text().isEmpty() ? r.text()
                    : r.tokens() != null ? String.join("", r.tokens()) : "";
            PluginExecuteResponse.Builder b = PluginExecuteResponse.newBuilder().setText(text)
                    .setFinishReason(r.finishReason() == null ? "stop" : r.finishReason())
                    .setInputTokens(r.inputTokens()).setOutputTokens(r.outputTokens());
            if (r.output() != null) b.setOutput(toStruct(r.output()));
            return b.build();
        }

        private static PluginExecuteResponse error(Exception e) {
            if (e instanceof PluginException p) {
                return PluginExecuteResponse.newBuilder().setIsError(true).setErrorCode(p.code()).setErrorMessage(p.getMessage()).build();
            }
            return PluginExecuteResponse.newBuilder().setIsError(true).setErrorCode("internal")
                    .setErrorMessage(e.getClass().getSimpleName() + ": " + e.getMessage()).build();
        }

        @Override
        public void execute(PluginExecuteRequest request, StreamObserver<PluginExecuteResponse> out) {
            PluginExecuteResponse res;
            try { res = fin(dispatch(request)); } catch (Exception e) { res = error(e); }
            out.onNext(res);
            out.onCompleted();
        }

        @Override
        public void executeStream(PluginExecuteRequest request, StreamObserver<PluginEvent> out) {
            try {
                Reply r = dispatch(request);
                List<String> tokens = r.tokens() != null ? r.tokens()
                        : (r.text() != null && !r.text().isEmpty() ? List.of(r.text()) : List.of());
                for (String t : tokens) out.onNext(PluginEvent.newBuilder().setToken(t).build());
                out.onNext(PluginEvent.newBuilder().setFinal(fin(r)).build());
            } catch (Exception e) {
                out.onNext(PluginEvent.newBuilder().setFinal(error(e)).build());
            }
            out.onCompleted();
        }

        @Override
        public void health(HealthRequest request, StreamObserver<HealthResponse> out) {
            out.onNext(HealthResponse.newBuilder().setServing(true).setMessage("ok").build());
            out.onCompleted();
        }

        @Override
        public void shutdown(ShutdownRequest request, StreamObserver<ShutdownResponse> out) {
            out.onNext(ShutdownResponse.getDefaultInstance());
            out.onCompleted();
            stop.countDown();
        }
    }

    /** Serve on UAR_PLUGIN_ADDR (default 127.0.0.1:50061) until Shutdown or JVM exit. */
    public static void serve(Definition d) throws IOException, InterruptedException {
        String addr = System.getenv().getOrDefault("UAR_PLUGIN_ADDR", "127.0.0.1:50061");
        int i = addr.lastIndexOf(':');
        CountDownLatch stop = new CountDownLatch(1);
        Server server = NettyServerBuilder.forAddress(new InetSocketAddress(addr.substring(0, i), Integer.parseInt(addr.substring(i + 1))))
                .addService(new Service(d, stop)).build().start();
        Runtime.getRuntime().addShutdownHook(new Thread(server::shutdown));
        System.err.println(d.id + "@" + d.version + " listening on " + addr);
        stop.await();
        server.shutdown();
    }
}
