// UAR inference sample (C#).
//
//   dotnet run -- "Explain quantum computing in two sentences"
//
// Sign-in, first match wins:
//   UAR_CLIENT_ID + UAR_CLIENT_SECRET   a registered application: exchanged for an access token
//   UAR_API_KEY                         an API key
// Optional: UAR_URL (default http://127.0.0.1:9000), UAR_MODEL (default local:default).
using System.Net.Http.Json;
using System.Text.Json.Nodes;
using Uar.Client;

var baseUrl = Environment.GetEnvironmentVariable("UAR_URL") ?? "http://127.0.0.1:9000";
var model = Environment.GetEnvironmentVariable("UAR_MODEL") ?? "local:default";
var prompt = args.Length > 0 ? string.Join(' ', args) : "Explain quantum computing in two sentences.";

if (string.IsNullOrEmpty(Environment.GetEnvironmentVariable("UAR_CLIENT_ID")) &&
    string.IsNullOrEmpty(Environment.GetEnvironmentVariable("UAR_API_KEY")))
{
    Console.Error.WriteLine("No credentials: set UAR_CLIENT_ID and UAR_CLIENT_SECRET (a registered application) or UAR_API_KEY. See samples/README.md.");
    return 2;
}

try
{
    var client = new UarClient(baseUrl);   // picks up UAR_API_KEY
    var who = "API key";
    if (Environment.GetEnvironmentVariable("UAR_CLIENT_ID") is { Length: > 0 } clientId)
    {
        client.ApiKey = null;
        client.Token = await AccessTokenAsync(baseUrl, clientId, Environment.GetEnvironmentVariable("UAR_CLIENT_SECRET") ?? "");
        who = $"application {clientId}";
    }
    Console.WriteLine($"UAR {baseUrl} | model {model} | signed in with {who}\n");
    var options = new InferenceOptions { MaxTokens = 300 };

    // 1. One request, one complete answer.
    var resp = await client.InferenceAsync(model, prompt, options);
    Console.WriteLine($"[{resp.Provider}/{resp.Model}] {resp.Text}");
    Console.WriteLine($"tokens: {resp.Usage?["input_tokens"]} in, {resp.Usage?["output_tokens"]} out\n");

    // 2. The same question, streamed token by token.
    Console.Write("streaming: ");
    await foreach (var ev in client.StreamAsync(model, prompt, options))
    {
        if (ev.Token is { } text) Console.Write(text);
        else if (ev.Type == "error") throw new Exception($"stream error: {ev.Raw["error"]?["message"]}");
    }
    Console.WriteLine();
    return 0;
}
catch (UarException e)   // e.g. 401 wrong credentials, 403 missing permission, 404 unknown model
{
    Console.Error.WriteLine($"UAR error {e.Status} {e.Code}: {e.Message}");
    return 1;
}

// Exchange an application's client credentials at the token service (OAuth 2.0 client_credentials).
static async Task<string> AccessTokenAsync(string baseUrl, string clientId, string secret)
{
    using var http = new HttpClient();
    var resp = await http.PostAsync($"{baseUrl}/api/v1/oauth/token", new FormUrlEncodedContent(new Dictionary<string, string>
    {
        ["grant_type"] = "client_credentials", ["client_id"] = clientId, ["client_secret"] = secret,
    }));
    var body = await resp.Content.ReadFromJsonAsync<JsonObject>();
    return body?["access_token"]?.GetValue<string>()
        ?? throw new Exception($"token request failed ({(int)resp.StatusCode}): {body?["error"]} {body?["error_description"]}");
}
