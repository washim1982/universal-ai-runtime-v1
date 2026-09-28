using System.Net.Http;
using System.Net.Http.Headers;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace UarAdmin.Services;

/// <summary>An error returned by the runtime ({"error": {code, message, ...}}) or a transport failure.</summary>
public sealed class ApiException : Exception
{
    public int Status { get; }
    public string Code { get; }
    public ApiException(int status, string code, string message) : base(message) { Status = status; Code = code; }
}

/// <summary>Thin client for the runtime's HTTP/JSON API, authenticated with the profile's API key.</summary>
public sealed class ApiClient : IDisposable
{
    readonly HttpClient http;
    readonly string? clientId, clientSecret;
    readonly SemaphoreSlim tokenLock = new(1, 1);
    string? accessToken;
    DateTimeOffset tokenExpires;
    public string BaseUrl { get; }
    public const string TokenPath = "api/v1/oauth/token";

    public static readonly JsonSerializerOptions Pretty = new() { WriteIndented = true };

    /// <summary>Authenticate as a registered application: access tokens are fetched from the runtime's
    /// token service and renewed before they expire.</summary>
    public ApiClient(string baseUrl, string clientId, string clientSecret) : this(baseUrl, "")
    {
        this.clientId = clientId;
        this.clientSecret = clientSecret;
    }

    public static ApiClient For(ConnectionProfile p) =>
        p.IsApp ? new ApiClient(p.BaseUrl, p.ClientId, p.ClientSecret) : new ApiClient(p.BaseUrl, p.ApiKey);

    public ApiClient(string baseUrl, string apiKey)
    {
        BaseUrl = baseUrl.TrimEnd('/');
        http = new HttpClient(new SocketsHttpHandler { PooledConnectionLifetime = TimeSpan.FromMinutes(2) })
        {
            BaseAddress = new Uri(BaseUrl + "/"),
            Timeout = TimeSpan.FromSeconds(20),
        };
        if (!string.IsNullOrEmpty(apiKey)) http.DefaultRequestHeaders.Add("X-API-Key", apiKey);
        http.DefaultRequestHeaders.UserAgent.Add(new ProductInfoHeaderValue("uar-admin", "0.9.0"));
    }

    public static string Query(string path, IDictionary<string, string?>? q)
    {
        if (q == null) return path;
        var parts = q.Where(kv => !string.IsNullOrEmpty(kv.Value))
                     .Select(kv => $"{Uri.EscapeDataString(kv.Key)}={Uri.EscapeDataString(kv.Value!)}").ToList();
        return parts.Count == 0 ? path : path + (path.Contains('?') ? "&" : "?") + string.Join("&", parts);
    }

    public Task<JsonNode> GetAsync(string path, IDictionary<string, string?>? query = null, CancellationToken ct = default) =>
        SendAsync(HttpMethod.Get, Query(path, query), null, ct);

    public Task<JsonNode> PostAsync(string path, object? body, CancellationToken ct = default) =>
        SendAsync(HttpMethod.Post, path, body is JsonNode n ? n.ToJsonString() : JsonSerializer.Serialize(body ?? new { }), ct);

    public async Task<JsonNode> SendAsync(HttpMethod method, string path, string? json, CancellationToken ct = default)
    {
        var (status, text) = await RawAsync(method, path, json, ct);
        JsonNode? node = null;
        try { node = string.IsNullOrWhiteSpace(text) ? new JsonObject() : JsonNode.Parse(text); } catch (JsonException) { }
        if (status >= 200 && status < 300) return node ?? new JsonObject();
        var err = node?["error"];
        if (status == 404 && err == null)   // route missing entirely: a runtime older than this app
            throw new ApiException(404, "runtime_outdated",
                "This runtime does not have this API yet: UAR Admin needs runtime 0.9 or newer. " +
                "Rebuild the Docker image (setup dashboard: Build the container image) or Stop it and Start the local runtime here.");
        throw new ApiException(status, err?["code"]?.GetValue<string>() ?? $"http_{status}",
                               err?["message"]?.GetValue<string>() ?? (text.Length > 300 ? text[..300] : text));
    }

    /// <summary>Status code and body text, without interpreting errors (used by the API explorer).</summary>
    public async Task<(int Status, string Body)> RawAsync(HttpMethod method, string path, string? json, CancellationToken ct = default)
    {
        var anonymous = path.TrimStart('/') is "healthz" or "readyz" || path.TrimStart('/').StartsWith(".well-known");
        for (var attempt = 0; ; attempt++)
        {
            using var req = new HttpRequestMessage(method, path.TrimStart('/'));
            if (json != null) req.Content = new StringContent(json, Encoding.UTF8, "application/json");
            if (clientId != null && !anonymous)
                req.Headers.Authorization = new AuthenticationHeaderValue("Bearer", await TokenAsync(attempt > 0, ct));
            var (status, body) = await SendRawAsync(req, ct);
            if (status == 401 && clientId != null && !anonymous && attempt == 0) continue;   // renew once
            return (status, body);
        }
    }

    /// <summary>The current access token of the application (renewed a minute before it expires).</summary>
    public async Task<string> TokenAsync(bool force = false, CancellationToken ct = default)
    {
        if (clientId == null) throw new InvalidOperationException("this connection uses an API key");
        await tokenLock.WaitAsync(ct);
        try
        {
            if (!force && accessToken != null && DateTimeOffset.UtcNow < tokenExpires.AddSeconds(-60)) return accessToken;
            var t = await RequestTokenAsync(clientId, clientSecret ?? "", null, ct);
            accessToken = t.AccessToken;
            tokenExpires = DateTimeOffset.UtcNow.AddSeconds(t.ExpiresIn);
            return accessToken;
        }
        finally { tokenLock.Release(); }
    }

    public sealed record TokenResponse(string AccessToken, int ExpiresIn, string Scope);

    /// <summary>OAuth 2.0 client_credentials at the runtime's token service (client_secret_basic).</summary>
    public async Task<TokenResponse> RequestTokenAsync(string id, string secret, string? scope, CancellationToken ct = default)
    {
        using var req = new HttpRequestMessage(HttpMethod.Post, TokenPath);
        var form = new Dictionary<string, string> { ["grant_type"] = "client_credentials" };
        if (!string.IsNullOrWhiteSpace(scope)) form["scope"] = scope;
        req.Content = new FormUrlEncodedContent(form);
        req.Headers.Authorization = new AuthenticationHeaderValue("Basic",
            Convert.ToBase64String(Encoding.UTF8.GetBytes($"{Uri.EscapeDataString(id)}:{Uri.EscapeDataString(secret)}")));
        var (status, body) = await SendRawAsync(req, ct);
        JsonNode? n = null;
        try { n = JsonNode.Parse(body); } catch (JsonException) { }
        if (status != 200 || n?["access_token"] == null)
            throw new ApiException(status, n.S("error", $"http_{status}"),
                n.S("error_description", status == 404 ? "this runtime has no token service (needs UAR 0.9+)" : "token request failed"));
        return new TokenResponse(n.S("access_token"), (int)n.L("expires_in"), n.S("scope"));
    }

    async Task<(int, string)> SendRawAsync(HttpRequestMessage req, CancellationToken ct)
    {
        try
        {
            using var resp = await http.SendAsync(req, ct);
            return ((int)resp.StatusCode, await resp.Content.ReadAsStringAsync(ct));
        }
        catch (HttpRequestException e)
        {
            throw new ApiException(0, "unreachable", $"cannot reach {BaseUrl}: {e.InnerException?.Message ?? e.Message}");
        }
        catch (TaskCanceledException) when (!ct.IsCancellationRequested)
        {
            throw new ApiException(0, "timeout", $"{BaseUrl} did not answer in time");
        }
    }

    public async Task<bool> HealthyAsync(CancellationToken ct = default)
    {
        try
        {
            var (status, _) = await RawAsync(HttpMethod.Get, "healthz", null, ct);
            return status == 200;
        }
        catch (ApiException) { return false; }
    }

    public void Dispose() => http.Dispose();
}

public static class Json
{
    public static string S(this JsonNode? n, string key, string fallback = "")
    {
        var v = n?[key];
        if (v == null) return fallback;
        return v is JsonValue jv ? jv.ToString() : v.ToJsonString();
    }

    public static long L(this JsonNode? n, string key)
    {
        var v = n?[key];
        if (v == null) return 0;
        return long.TryParse(v.ToString(), out var x) ? x : 0;
    }

    public static bool B(this JsonNode? n, string key) => n?[key] is JsonValue v && v.TryGetValue<bool>(out var b) && b;

    public static IEnumerable<JsonNode> Arr(this JsonNode? n, string key) =>
        (n?[key] as JsonArray)?.Where(x => x != null).Select(x => x!) ?? Enumerable.Empty<JsonNode>();

    public static string Pretty(JsonNode? n) => n == null ? "" : n.ToJsonString(ApiClient.Pretty);

    public static string Local(string iso)
    {
        if (string.IsNullOrEmpty(iso)) return "";
        return DateTimeOffset.TryParse(iso, out var t) ? t.ToLocalTime().ToString("yyyy-MM-dd HH:mm:ss") : iso;
    }
}
