// The UAR .NET example (reference sdks/dotnet/Uar.Client).
using Uar.Client;

var client = new UarClient("http://localhost:9000");   // key from UAR_API_KEY
var resp = await client.InferenceAsync("local:default", "Explain quantum computing", agent: "in_app_assistant");
Console.WriteLine(resp.Text);

await foreach (var ev in client.StreamAsync("local:default", "Name three planets."))
    if (ev.Token is { } t) Console.Write(t);
