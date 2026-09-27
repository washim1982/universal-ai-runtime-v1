// Conformance: the shared fixtures in contracts/fixtures through the .NET SDK, against uar-mock (default)
// or a live runtime (UAR_LIVE_URL + UAR_LIVE_KEY). Exit code 0 = all passed.
using System.Diagnostics;
using System.Net;
using System.Net.Sockets;
using System.Text.Json;
using System.Text.Json.Nodes;
using Uar.Client;

var root = Path.GetFullPath(Path.Combine(AppContext.BaseDirectory, "..", "..", "..", "..", "..", ".."));
if (!Directory.Exists(Path.Combine(root, "contracts"))) root = Path.GetFullPath(Path.Combine(Environment.CurrentDirectory, "..", ".."));
var fixtures = Directory.GetFiles(Path.Combine(root, "contracts", "fixtures"), "*.json")
    .Select(f => JsonNode.Parse(File.ReadAllText(f))!.AsObject())
    .ToDictionary(f => f["name"]!.GetValue<string>());

string url, key;
Process? mock = null;
if (Environment.GetEnvironmentVariable("UAR_LIVE_URL") is { Length: > 0 } live)
{
    url = live; key = Environment.GetEnvironmentVariable("UAR_LIVE_KEY") ?? "";
}
else
{
    var l = new TcpListener(IPAddress.Loopback, 0); l.Start(); var port = ((IPEndPoint)l.LocalEndpoint).Port; l.Stop();
    var venv = OperatingSystem.IsWindows() ? Path.Combine(root, ".venv", "Scripts", "python.exe") : Path.Combine(root, ".venv", "bin", "python");
    var py = Environment.GetEnvironmentVariable("UAR_PYTHON") ?? (File.Exists(venv) ? venv : "python");
    mock = Process.Start(new ProcessStartInfo(py, $"\"{Path.Combine(root, "mock", "uar_mock.py")}\" --port {port}") { UseShellExecute = false });
    url = $"http://127.0.0.1:{port}"; key = "uar_mock0000_notasecretjustamockkey";
    using var probe = new HttpClient();
    for (var i = 0; i < 100; i++) { try { await probe.GetAsync(url + "/"); break; } catch { await Task.Delay(100); } }
}

var failures = new List<string>();
var done = new HashSet<string>();
var client = new UarClient(url, key);

JsonNode? Norm(JsonNode? n) => n switch
{
    JsonObject o => new JsonObject(o.Where(kv => kv.Value is not null).Select(kv => KeyValuePair.Create(kv.Key, Norm(kv.Value)))),
    JsonArray a => new JsonArray(a.Select(Norm).ToArray()),
    JsonValue v when v.TryGetValue<double>(out var d) => JsonValue.Create(d),
    JsonValue v when v.GetValueKind() == JsonValueKind.Number => JsonValue.Create(v.GetValue<double>()),
    _ => n?.DeepClone(),
};

JsonNode? Project(JsonNode? have, JsonNode? want) => (have, want) switch
{
    (JsonObject h, JsonObject w) => new JsonObject(w.Select(kv => KeyValuePair.Create(kv.Key,
        Project(h[kv.Key]?.DeepClone() ?? Default(kv.Value), kv.Value)))),
    (JsonArray h, JsonArray w) when h.Count == w.Count => new JsonArray(h.Select((x, i) => Project(x?.DeepClone(), w[i])).ToArray()),
    _ => have?.DeepClone(),
};

JsonNode? Default(JsonNode? w) => w?.GetValueKind() switch
{
    JsonValueKind.True or JsonValueKind.False => JsonValue.Create(false),
    JsonValueKind.String => JsonValue.Create(""),
    JsonValueKind.Number => JsonValue.Create(0.0),
    JsonValueKind.Array => new JsonArray(),
    _ => null,
};

void Strip(JsonNode? node, IEnumerable<string> ignore)
{
    foreach (var path in ignore)
    {
        var parts = path.Split('.');
        var cur = node;
        foreach (var p in parts[..^1]) cur = cur?[p];
        (cur as JsonObject)?.Remove(parts[^1]);
    }
}

void Expect(string name, JsonNode? have, JsonNode? want = null)
{
    var fx = fixtures[name]["response"]!;
    want = Norm(want ?? fx["body"]);
    have = Norm(have);
    var ignore = fx["ignore"]!.AsArray().Select(x => x!.GetValue<string>()).ToList();
    Strip(want, ignore); Strip(have, ignore);
    var projected = Project(have, want);
    if (!JsonNode.DeepEquals(projected, want))
        failures.Add($"{name}: want {want!.ToJsonString()}\n  have {projected!.ToJsonString()}");
}

JsonNode? Ser(object o) => JsonSerializer.SerializeToNode(o, o.GetType(), UarClient.Json);

async Task ExpectError(string name, Func<Task> call, int status, string code)
{
    try { await call(); failures.Add($"{name}: no error"); }
    catch (UarException e) when (e.Status == status && e.Code == code) { }
    catch (Exception e) { failures.Add($"{name}: {e.Message}"); }
}

async Task Case(string name, Func<Task> body)
{
    done.Add(name);
    try { await body(); } catch (Exception e) { failures.Add($"{name}: {e.GetType().Name}: {e.Message}"); }
}

await Case("inference_basic", async () =>
{
    var r = await client.InferenceAsync("local:default", "hello");
    if (r.Text != "echo: hello") failures.Add("inference_basic: text");
    Expect("inference_basic", Ser(r));
});
await Case("inference_stream", async () =>
{
    var events = new List<UarEvent>();
    await foreach (var ev in client.StreamAsync("local:default", "stream")) events.Add(ev);
    var want = fixtures["inference_stream"]["response"]!["events"]!.AsArray();
    if (events.Count != want.Count) { failures.Add($"inference_stream: {events.Count} events"); return; }
    if (string.Concat(events.Select(e => e.Token)) != "echo: stream") failures.Add("inference_stream: text");
    for (var i = 0; i < want.Count; i++)
    {
        var raw = (JsonObject)events[i].Raw.DeepClone(); raw.Remove("type");
        Expect("inference_stream", raw, want[i]);
    }
});
await Case("error_unauthenticated", () =>
    ExpectError("error_unauthenticated", () => new UarClient(url, "").InferenceAsync("local:default", "hi"), 401, "unauthenticated"));
await Case("tool_execute_read", async () =>
    Expect("tool_execute_read", Ser(await client.ExecuteToolAsync("fs.read_text", new { path = "docs/faq.md" }))));
await Case("tool_policy_denied", () =>
    ExpectError("tool_policy_denied", () => client.ExecuteToolAsync("fs.write_text", new { path = "docs/x.md", content = "x" }), 403, "policy_denied"));
await Case("run_not_found", () =>
    ExpectError("run_not_found", () => client.GetRunAsync("run_does_not_exist"), 404, "not_found"));
await Case("run_start", async () =>
    Expect("run_start", Ser(await client.RunAgentAsync("in_app_assistant", new { prompt = "What is the return window?" }))));
await Case("dry_run_static", async () =>
{
    var r = await client.DryRunAsync(new { agent_id = "in_app_assistant", mode = "static" });
    if (!r.ExecutedNothing) failures.Add("dry_run_static: executed_nothing");
    Expect("dry_run_static", Ser(r));
});
foreach (var name in fixtures.Keys.Where(k => !done.Contains(k))) failures.Add($"no SDK test for fixture {name}");

// No automatic retry without an idempotency key.
{
    var listener = new HttpListener();
    var l = new TcpListener(IPAddress.Loopback, 0); l.Start(); var port = ((IPEndPoint)l.LocalEndpoint).Port; l.Stop();
    listener.Prefixes.Add($"http://127.0.0.1:{port}/"); listener.Start();
    var calls = 0;
    _ = Task.Run(async () =>
    {
        while (listener.IsListening)
        {
            HttpListenerContext ctx;
            try { ctx = await listener.GetContextAsync(); } catch { break; }
            Interlocked.Increment(ref calls);
            ctx.Response.StatusCode = 503;
            var b = "{\"error\":{\"code\":\"unavailable\",\"message\":\"x\",\"retryable\":true}}"u8.ToArray();
            ctx.Response.ContentType = "application/json";
            await ctx.Response.OutputStream.WriteAsync(b);
            ctx.Response.Close();
        }
    });
    var c = new UarClient($"http://127.0.0.1:{port}", "k") { MaxRetries = 3 };
    await ExpectError("no_retry", () => c.InferenceAsync("m", "p"), 503, "unavailable");
    if (calls != 1) failures.Add($"inference retried: {calls} calls");
    calls = 0;
    try { await c.ExecuteToolAsync("t", new { }, "k1"); } catch (UarException) { }
    if (calls != 4) failures.Add($"idempotent call attempts = {calls}, want 4");
    listener.Stop();
}

mock?.Kill(true);
foreach (var f in failures) Console.Error.WriteLine("FAIL " + f);
Console.WriteLine(failures.Count == 0 ? $"{done.Count} fixtures passed + retry check" : $"{failures.Count} failure(s)");
return failures.Count == 0 ? 0 : 1;
