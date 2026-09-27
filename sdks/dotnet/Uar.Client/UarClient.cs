// .NET client for the Universal AI Runtime (HTTP/JSON + SSE).
//
//   var client = new UarClient("http://localhost:9000");                    // key from UAR_API_KEY
//   var resp = await client.InferenceAsync("local:default", "Explain quantum computing", agent: "research_agent");
//   Console.WriteLine(resp.Text);
//
// Only GET requests and requests with an idempotency key are retried (429, 503, network errors);
// inference and tool calls without a key never are.
using System.Net;
using System.Net.Http.Headers;
using System.Net.Http.Json;
using System.Runtime.CompilerServices;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.Json.Serialization;

namespace Uar.Client;

/// <summary>A structured runtime error ({code, message, request_id, retryable, details}).</summary>
public sealed class UarException : Exception
{
    public int Status { get; }
    public string Code { get; }
    public string RequestId { get; }
    public bool Retryable { get; }
    public JsonNode? Details { get; }

    public UarException(int status, string code, string message, string requestId, bool retryable, JsonNode? details)
        : base($"{code}: {message} (status {status}, request {requestId})")
    {
        Status = status; Code = code; RequestId = requestId; Retryable = retryable; Details = details;
    }

    public bool IsAuthentication => Status == 401;
    public bool IsPermissionDenied => Status == 403;
    public bool IsNotFound => Status == 404;
    public bool IsRateLimited => Status == 429;
}

/// <summary>Base for response types: unknown fields are kept so a response round-trips exactly.</summary>
public abstract record UarMessage
{
    [JsonExtensionData] public Dictionary<string, JsonElement>? Extra { get; init; }
}

public sealed record InferenceResponse : UarMessage
{
    public string RequestId { get; init; } = "";
    public string Provider { get; init; } = "";
    public string Model { get; init; } = "";
    public string Content { get; init; } = "";
    public string FinishReason { get; init; } = "";
    public string RunId { get; init; } = "";
    public JsonArray? ToolCalls { get; init; }
    public JsonObject? Usage { get; init; }
    public JsonObject? Route { get; init; }
    public JsonObject? Output { get; init; }
    /// <summary>The generated text (alias of Content).</summary>
    [JsonIgnore] public string Text => Content;
}

public sealed record Run : UarMessage
{
    public string RunId { get; init; } = "";
    public string AgentId { get; init; } = "";
    public string Version { get; init; } = "";
    public string Status { get; init; } = "";
    public int Steps { get; init; }
    public string CurrentNode { get; init; } = "";
    public JsonObject? Output { get; init; }
    public JsonObject? Error { get; init; }
    public JsonObject? Usage { get; init; }
    public string CreatedAt { get; init; } = "";
    public string UpdatedAt { get; init; } = "";
    public string ParentRunId { get; init; } = "";
    public bool CancelRequested { get; init; }
}

public sealed record ToolResult : UarMessage
{
    public string RequestId { get; init; } = "";
    public string Tool { get; init; } = "";
    public bool IsError { get; init; }
    public JsonArray? Content { get; init; }
    public JsonObject? Structured { get; init; }
    public bool Truncated { get; init; }
    public bool Untrusted { get; init; }
    public int DurationMs { get; init; }
}

public sealed record DryRunReport : UarMessage
{
    public string RequestId { get; init; } = "";
    public string Mode { get; init; } = "";
    public bool Valid { get; init; }
    public List<string> Errors { get; init; } = new();
    public List<string> Warnings { get; init; } = new();
    public JsonArray? Steps { get; init; }
    public JsonArray? Routes { get; init; }
    public List<string> Unresolved { get; init; } = new();
    public List<string> Branches { get; init; } = new();
    public bool ExecutedNothing { get; init; }
}

public sealed record AgentVersion : UarMessage
{
    public string AgentId { get; init; } = "";
    public string Version { get; init; } = "";
    public string Digest { get; init; } = "";
    public string CreatedAt { get; init; } = "";
}

/// <summary>One stream event; Type is the populated body ("started", "token", "usage", "completed", "error").</summary>
public sealed record UarEvent(string Type, int Seq, JsonObject Raw)
{
    public JsonNode? Body => Raw[Type];
    public string? Token => Type == "token" ? Raw["token"]?["text"]?.GetValue<string>() : null;
}

public sealed class InferenceOptions
{
    public List<object>? Messages { get; init; }
    public string? Agent { get; init; }
    public List<string>? Tools { get; init; }
    public string? ToolMode { get; init; }
    public double? Temperature { get; init; }
    public int? MaxTokens { get; init; }
    public object? ResponseSchema { get; init; }
    public string? DataClass { get; init; }
    public object? Extensions { get; init; }
}

public sealed class UarClient : IDisposable
{
    public static readonly JsonSerializerOptions Json = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
        DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull,
    };
    public const string Version = "0.8.0";

    private readonly HttpClient _http;
    private readonly string _base;
    public string? ApiKey { get; set; }
    public string? Token { get; set; }
    public int MaxRetries { get; set; } = 2;

    public UarClient(string baseUrl = "http://localhost:9000", string? apiKey = null, HttpClient? http = null)
    {
        _base = baseUrl.TrimEnd('/');
        ApiKey = apiKey ?? Environment.GetEnvironmentVariable("UAR_API_KEY");
        _http = http ?? new HttpClient { Timeout = Timeout.InfiniteTimeSpan };
    }

    public void Dispose() => _http.Dispose();

    private HttpRequestMessage Build(HttpMethod method, string path, object? body, string? idem, string accept)
    {
        var req = new HttpRequestMessage(method, _base + path);
        req.Headers.Accept.Add(new MediaTypeWithQualityHeaderValue(accept));
        req.Headers.UserAgent.ParseAdd($"uar-dotnet/{Version}");
        if (!string.IsNullOrEmpty(ApiKey)) req.Headers.Add("X-API-Key", ApiKey);
        else if (!string.IsNullOrEmpty(Token)) req.Headers.Authorization = new AuthenticationHeaderValue("Bearer", Token);
        if (idem is not null) req.Headers.Add("Idempotency-Key", idem);
        if (body is not null) req.Content = JsonContent.Create(body, body.GetType(), options: Json);
        return req;
    }

    private static async Task<UarException> ErrorFrom(HttpResponseMessage resp, CancellationToken ct)
    {
        var text = await resp.Content.ReadAsStringAsync(ct);
        JsonNode? err = null;
        try { err = JsonNode.Parse(text)?["error"]; } catch (JsonException) { }
        string S(string k) => err?[k]?.GetValue<string>() ?? "";
        return err is null
            ? new UarException((int)resp.StatusCode, "http_error", text.Length > 300 ? text[..300] : text, "", false, null)
            : new UarException((int)resp.StatusCode, S("code"), S("message"), S("request_id"),
                               err["retryable"]?.GetValue<bool>() ?? false, err["details"]?.DeepClone());
    }

    private async Task<T> Call<T>(HttpMethod method, string path, object? body = null, string? idem = null,
                                  CancellationToken ct = default)
    {
        var retryable = method == HttpMethod.Get || idem is not null;
        for (var attempt = 0; ; attempt++)
        {
            HttpResponseMessage? resp = null;
            try
            {
                resp = await _http.SendAsync(Build(method, path, body, idem, "application/json"), ct);
                if (resp.IsSuccessStatusCode)
                    return (await resp.Content.ReadFromJsonAsync<T>(Json, ct))!;
                var status = (int)resp.StatusCode;
                if (!(retryable && attempt < MaxRetries && (status == 429 || status == 503)))
                    throw await ErrorFrom(resp, ct);
            }
            catch (HttpRequestException) when (retryable && attempt < MaxRetries) { }
            var delay = resp?.Headers.RetryAfter?.Delta ?? TimeSpan.FromMilliseconds(250 * (1 << attempt));
            if (delay > TimeSpan.FromSeconds(30)) delay = TimeSpan.FromSeconds(30);
            await Task.Delay(delay * (0.5 + Random.Shared.NextDouble()), ct);
        }
    }

    private static object InferenceBody(string model, string prompt, InferenceOptions? o, bool stream)
    {
        var b = new Dictionary<string, object?> { ["model"] = model };
        if (o?.Messages is { Count: > 0 }) b["messages"] = o.Messages; else b["input"] = prompt;
        if (o?.Agent is not null) b["agent"] = o.Agent;
        if (o?.Tools is not null) b["tools"] = o.Tools;
        if (o?.ToolMode is not null) b["tool_mode"] = o.ToolMode;
        if (o?.DataClass is not null) b["data_class"] = o.DataClass;
        if (o?.Extensions is not null) b["extensions"] = o.Extensions;
        var p = new Dictionary<string, object?>();
        if (o?.Temperature is not null) p["temperature"] = o.Temperature;
        if (o?.MaxTokens is not null) p["max_tokens"] = o.MaxTokens;
        if (o?.ResponseSchema is not null) p["response_schema"] = o.ResponseSchema;
        if (p.Count > 0) b["params"] = p;
        if (stream) b["stream"] = true;
        return b;
    }

    // ------------------------------------------------------------ inference

    /// <summary>Synchronous inference. With an agent, runs that agent on the prompt and returns its result.</summary>
    public Task<InferenceResponse> InferenceAsync(string model, string prompt, string? agent = null,
                                                  CancellationToken ct = default)
        => InferenceAsync(model, prompt, new InferenceOptions { Agent = agent }, ct);

    public Task<InferenceResponse> InferenceAsync(string model, string prompt, InferenceOptions? options,
                                                  CancellationToken ct = default)
        => Call<InferenceResponse>(HttpMethod.Post, "/api/v1/inference", InferenceBody(model, prompt, options, false), ct: ct);

    /// <summary>Streaming inference; disposing the enumerator closes the connection and stops generation.</summary>
    public IAsyncEnumerable<UarEvent> StreamAsync(string model, string prompt, InferenceOptions? options = null,
                                                  CancellationToken ct = default)
        => Sse(HttpMethod.Post, "/api/v1/inference", InferenceBody(model, prompt, options, true), ct);

    private async IAsyncEnumerable<UarEvent> Sse(HttpMethod method, string path, object? body,
                                                 [EnumeratorCancellation] CancellationToken ct = default)
    {
        using var resp = await _http.SendAsync(Build(method, path, body, null, "text/event-stream"),
                                               HttpCompletionOption.ResponseHeadersRead, ct);
        if (!resp.IsSuccessStatusCode) throw await ErrorFrom(resp, ct);
        using var reader = new StreamReader(await resp.Content.ReadAsStreamAsync(ct), Encoding.UTF8);
        var data = new StringBuilder();
        while (await reader.ReadLineAsync(ct) is { } line)
        {
            if (line.Length == 0)
            {
                if (data.Length > 0) { yield return Parse(data.ToString()); data.Clear(); }
            }
            else if (line.StartsWith("data:"))
            {
                if (data.Length > 0) data.Append('\n');
                data.Append(line[5..].TrimStart());
            }
        }
        if (data.Length > 0) yield return Parse(data.ToString());
    }

    private static UarEvent Parse(string json)
    {
        var o = JsonNode.Parse(json)!.AsObject();
        return new UarEvent(o["type"]?.GetValue<string>() ?? "", o["seq"]?.GetValue<int>() ?? 0, o);
    }

    // ------------------------------------------------------------ tools & catalog

    public Task<ToolResult> ExecuteToolAsync(string tool, object args, string? idempotencyKey = null,
                                             CancellationToken ct = default)
        => Call<ToolResult>(HttpMethod.Post, "/api/v1/tool/execute", new { tool, args }, idempotencyKey, ct);

    public async Task<JsonArray> ListModelsAsync(CancellationToken ct = default)
        => (await Call<JsonObject>(HttpMethod.Get, "/api/v1/models", ct: ct))["models"]!.AsArray();

    public async Task<JsonArray> ListToolsAsync(CancellationToken ct = default)
        => (await Call<JsonObject>(HttpMethod.Get, "/api/v1/tools", ct: ct))["tools"]!.AsArray();

    // ------------------------------------------------------------ agents & runs

    public Task<AgentVersion> RegisterAgentAsync(object definition, CancellationToken ct = default)
        => Call<AgentVersion>(HttpMethod.Post, "/api/v1/agents", new { definition }, ct: ct);

    /// <summary>Start a run (202). Pass an idempotency key to make it safe to retry.</summary>
    public Task<Run> RunAgentAsync(string agentId, object? input = null, string? idempotencyKey = null,
                                   CancellationToken ct = default)
        => Call<Run>(HttpMethod.Post, "/api/v1/agent/run",
                     new Dictionary<string, object?> { ["agent_id"] = agentId, ["input"] = input ?? new { } },
                     idempotencyKey, ct);

    public Task<Run> GetRunAsync(string runId, CancellationToken ct = default)
        => Call<Run>(HttpMethod.Get, $"/api/v1/runs/{Uri.EscapeDataString(runId)}", ct: ct);

    public async Task<Run> WaitRunAsync(string runId, CancellationToken ct = default)
    {
        while (true)
        {
            var r = await GetRunAsync(runId, ct);
            if (r.Status is "succeeded" or "failed" or "cancelled" or "needs_attention") return r;
            await Task.Delay(250, ct);
        }
    }

    public IAsyncEnumerable<UarEvent> WatchRunAsync(string runId, int afterSeq = 0, CancellationToken ct = default)
        => Sse(HttpMethod.Get, $"/api/v1/runs/{Uri.EscapeDataString(runId)}/events?after_seq={afterSeq}", null, ct);

    public Task<Run> CancelRunAsync(string runId, string reason = "", CancellationToken ct = default)
        => Call<Run>(HttpMethod.Post, $"/api/v1/runs/{Uri.EscapeDataString(runId)}/cancel", new { reason }, ct: ct);

    public Task<Run> ResolveRunAsync(string runId, string action, string note = "", CancellationToken ct = default)
        => Call<Run>(HttpMethod.Post, $"/api/v1/runs/{Uri.EscapeDataString(runId)}/resolve", new { action, note }, ct: ct);

    /// <summary>Preview an agent without executing anything (request: a DryRunRequest object).</summary>
    public Task<DryRunReport> DryRunAsync(object request, CancellationToken ct = default)
        => Call<DryRunReport>(HttpMethod.Post, "/api/v1/dry-run", request, ct: ct);
}
