using System.IO;
using System.Net.Http;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using Microsoft.Win32;
using UarAdmin.Services;

namespace UarAdmin.Views;

public partial class ApiReferencePage : UserControl, IPage
{
    readonly Shell shell;
    JsonNode? spec;
    string rawSpec = "";
    List<Op> ops = new();
    string exampleModel = "local:ollama/granite4";

    public sealed record Op(string Method, string Path, string OperationId, string Description, JsonNode Node)
    {
        public Brush MethodBg => new SolidColorBrush(Method == "GET" ? Color.FromRgb(0x25, 0x63, 0xEB) : Color.FromRgb(0x16, 0xA3, 0x4A));
    }

    public ApiReferencePage(Shell shell)
    {
        this.shell = shell;
        InitializeComponent();
    }

    public async Task ShownAsync()
    {
        if (spec == null) await LoadAsync();
    }

    async Task LoadAsync()
    {
        if (shell.Api == null) return;
        await shell.Try(async () =>
        {
            var (status, body) = await shell.Api.RawAsync(HttpMethod.Get, "api/v1/openapi.json", null);
            if (status != 200) throw new ApiException(status, "openapi", $"openapi.json returned HTTP {status}");
            rawSpec = body;
            spec = JsonNode.Parse(body);
            ops = new();
            foreach (var (path, item) in spec!["paths"]!.AsObject())
                foreach (var (method, op) in item!.AsObject())
                    ops.Add(new Op(method.ToUpperInvariant(), path, op.S("operationId"), op.S("description"), op!));
            ops = ops.OrderBy(o => o.Path).ThenBy(o => o.Method).ToList();
            try
            {
                var models = (await shell.Api.GetAsync("api/v1/models")).Arr("models").ToList();
                var pick = models.FirstOrDefault(m => m.B("available") && m.S("model_class") == "local") ?? models.FirstOrDefault(m => m.B("available"));
                if (pick != null) exampleModel = pick.S("name");
            }
            catch (ApiException) { }
            Subtitle.Text = $"{spec["info"].S("title")} {spec["info"].S("version")} · {ops.Count} operations · generated from the canonical proto contract; gRPC offers the same operations.";
            ApplyFilter();
            if (ops.Count > 0) Ops.SelectedIndex = 0;
        });
    }

    void ApplyFilter()
    {
        var q = Search.Text.Trim();
        Ops.ItemsSource = string.IsNullOrEmpty(q) ? ops :
            ops.Where(o => (o.Path + " " + o.OperationId + " " + o.Description).Contains(q, StringComparison.OrdinalIgnoreCase)).ToList();
    }

    void Search_Changed(object sender, TextChangedEventArgs e) => ApplyFilter();

    async void Reload_Click(object sender, RoutedEventArgs e) => await LoadAsync();

    void Ops_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (Ops.SelectedItem is not Op op || spec == null) return;
        MethodText.Text = op.Method;
        MethodBadge.Background = op.MethodBg;
        PathText.Text = op.Path;
        DescText.Text = $"{op.Description}  (operation {op.OperationId})";
        var ps = op.Node.Arr("parameters").Select(p => $"{p.S("in"),-6} {p.S("name")}{(p.B("required") ? " (required)" : "")} : {p["schema"].S("type")}").ToList();
        ParamsText.Text = ps.Count == 0 ? "" : "Parameters\n" + string.Join("\n", ps);
        var reqRef = op.Node["requestBody"]?["content"]?["application/json"]?["schema"];
        var content = op.Node["responses"]?["200"]?["content"]?.AsObject();
        var respRef = content?.FirstOrDefault().Value?["schema"];
        var media = content?.FirstOrDefault().Key ?? "application/json";
        RequestSchema.Text = reqRef == null ? "(no request body)" : Describe(reqRef);
        ResponseSchema.Text = respRef == null ? "(no body)" : $"// {media}\n" + Describe(respRef);
        TryPath.Text = op.Path;
        TryBody.Text = reqRef == null ? "" : Json.Pretty(Sample(op.OperationId) ?? Example(reqRef, 0));
        TryBody.IsEnabled = reqRef != null;
        TryStatus.Foreground = (Brush)FindResource("Muted");
        TryStatus.Text = (op.Method == "GET" ? "Replace any {placeholders} in the path, then Send." :
                          Sample(op.OperationId) != null ? "A working example body is filled in. POST changes state: Send asks for confirmation." :
                          "Edit the body (the template lists every field; remove what you don't need). POST asks for confirmation.") +
                         $"  Authentication is automatic: the key of connection \"{shell.Profile?.Name}\" is sent.";
        TryResult.Text = "";
        var baseUrl = shell.Api?.BaseUrl ?? "http://127.0.0.1:9000";
        CurlText.Text = op.Method == "GET"
            ? $"curl -H \"X-API-Key: $UAR_KEY\" \"{baseUrl}{op.Path}\""
            : $"curl -X POST -H \"X-API-Key: $UAR_KEY\" -H \"Content-Type: application/json\" \\\n  -d '{(reqRef == null ? "{}" : (Sample(op.OperationId) ?? Example(reqRef, 0))!.ToJsonString())}' \\\n  \"{baseUrl}{op.Path}\"";
    }

    string Describe(JsonNode schema)
    {
        var name = schema.S("$ref").Split('/').LastOrDefault();
        var target = Resolve(schema);
        var sb = new StringBuilder();
        if (!string.IsNullOrEmpty(name)) sb.AppendLine($"// {name}{(target.S("description") != "" ? " - " + target.S("description") : "")}");
        sb.AppendLine("// example shape (every field is optional in proto3 JSON unless noted)");
        sb.AppendLine(Json.Pretty(Example(schema, 0)));
        sb.AppendLine();
        sb.AppendLine("// fields");
        foreach (var (field, node) in (target["properties"] as JsonObject ?? new JsonObject()))
        {
            if (node == null) continue;
            var items = node["items"];
            string Name(JsonNode? n) => n.S("$ref") != "" ? n.S("$ref").Split('/').Last() : n.S("type");
            var t = node.S("type") == "array" ? $"array of {Name(items)}" : Name(node);
            var d = node.S("description");
            sb.AppendLine($"  {field,-28} {t}{(d != "" ? "   " + d : "")}");
        }
        return sb.ToString();
    }

    /// <summary>A minimal request that works against the example setup, for the common operations.</summary>
    JsonNode? Sample(string operationId) => operationId switch
    {
        "Infer" or "InferStream" => new JsonObject { ["model"] = exampleModel, ["input"] = "Say hello in one sentence." },
        "ExecuteTool" => JsonNode.Parse("""{"tool": "fs.read_text", "args": {"path": "docs/product-faq.md"}}"""),
        "StartRun" => JsonNode.Parse("""{"agent_id": "in_app_assistant", "input": {"prompt": "How long do I have to return a product?"}}"""),
        "DryRun" => JsonNode.Parse("""{"agent_id": "report_generator", "input": {"report_name": "sales_q3", "quarter": "2026-Q3"}}"""),
        "RegisterAgent" => JsonNode.Parse("""
            {"definition": {"apiVersion": "uar/v1", "kind": "Agent", "metadata": {"id": "hello_agent", "version": "1.0.0"},
             "spec": {"start": "ask", "permissions": {"models": ["local:*"], "tools": [], "agents": []},
                      "nodes": [{"id": "ask", "type": "llm", "model": "__MODEL__", "prompt": "Greet ${input.name} in one sentence."},
                                {"id": "done", "type": "return", "value": {"text": "${nodes.ask.output.text}"}}],
                      "edges": [{"from": "ask", "to": "done"}]}}}
            """.Replace("__MODEL__", exampleModel)),
        "CreateApiKey" => JsonNode.Parse("""{"subject": "demo-app@acme", "roles": ["viewer"], "description": "created from API reference", "expires_in_days": 7}"""),
        "RevokeApiKey" => JsonNode.Parse("""{"reason": "rotated"}"""),
        "DecideApproval" => JsonNode.Parse("""{"approve": true, "comment": "looks right"}"""),
        "CancelRun" => JsonNode.Parse("""{"reason": "no longer needed"}"""),
        "ResolveRun" => JsonNode.Parse("""{"action": "retry_node", "note": "checked the target system"}"""),
        "ActivatePlugin" => JsonNode.Parse("""{"version": "1.0.0"}"""),
        "RollbackPlugin" => new JsonObject(),
        _ => null,
    };

    JsonNode Resolve(JsonNode schema)
    {
        var r = schema.S("$ref");
        if (r == "" || spec == null) return schema;
        return spec["components"]?["schemas"]?[r.Split('/').Last()] ?? schema;
    }

    JsonNode? Example(JsonNode? schema, int depth)
    {
        if (schema == null) return null;
        var s = Resolve(schema);
        var type = s["type"] is JsonArray ta ? ta[0]?.ToString() : s.S("type");
        if (s["properties"] is JsonObject props)
        {
            if (depth > 2) return new JsonObject();
            var o = new JsonObject();
            var oneofs = s["x-oneof"] as JsonObject;
            var skip = new HashSet<string>(oneofs?.SelectMany(kv => (kv.Value as JsonArray)!.Skip(1).Select(x => x!.ToString())) ?? Enumerable.Empty<string>());
            foreach (var (k, v) in props)
                if (!skip.Contains(k) && k != "type") o[k] = Example(v, depth + 1);
            return o;
        }
        return type switch
        {
            "array" => depth > 2 ? new JsonArray() : new JsonArray(Example(s["items"], depth + 1)),
            "integer" => 0,
            "number" => 0.0,
            "boolean" => false,
            "object" => new JsonObject(),
            _ => s.S("format") == "date-time" ? "2026-01-01T00:00:00Z" : "string",
        };
    }

    async void Send_Click(object sender, RoutedEventArgs e)
    {
        if (Ops.SelectedItem is not Op op || shell.Api == null) return;
        if (op.Method != "GET" && !Shell.Confirm($"Send {op.Method} {TryPath.Text}? This runs against the live runtime with the current connection's key.")) return;
        string? body = null;
        if (op.Method != "GET")
        {
            try { body = string.IsNullOrWhiteSpace(TryBody.Text) ? "{}" : JsonNode.Parse(TryBody.Text)!.ToJsonString(); }
            catch (JsonException ex) { TryStatus.Text = $"Body is not valid JSON: {ex.Message}"; return; }
        }
        SendBtn.IsEnabled = false;
        TryStatus.Text = "Sending…";
        var started = DateTime.Now;
        try
        {
            var (status, text) = await shell.Api.RawAsync(new HttpMethod(op.Method), TryPath.Text.Trim(), body);
            TryStatus.Text = $"HTTP {status} · {(DateTime.Now - started).TotalMilliseconds:N0} ms";
            TryStatus.Foreground = (Brush)FindResource(status < 300 ? "Success" : "Danger");
            try { TryResult.Text = Json.Pretty(JsonNode.Parse(text)); } catch (JsonException) { TryResult.Text = text; }
        }
        catch (ApiException ex) { TryStatus.Text = ex.Message; }
        finally { SendBtn.IsEnabled = true; }
    }

    void CopyCurl_Click(object sender, RoutedEventArgs e) { Clipboard.SetText(CurlText.Text); shell.Notify("curl command copied."); }

    void Save_Click(object sender, RoutedEventArgs e)
    {
        if (rawSpec == "") return;
        var dlg = new SaveFileDialog { FileName = "openapi.json", Filter = "JSON|*.json" };
        if (dlg.ShowDialog() == true) { File.WriteAllText(dlg.FileName, Json.Pretty(JsonNode.Parse(rawSpec))); shell.Notify($"Saved {dlg.FileName}.", Notice.Success); }
    }
}
