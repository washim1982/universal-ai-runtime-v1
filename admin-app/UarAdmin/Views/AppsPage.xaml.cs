using System.Text.Json.Nodes;
using System.Windows;
using System.Windows.Controls;
using UarAdmin.Dialogs;
using UarAdmin.Services;

namespace UarAdmin.Views;

public partial class AppsPage : UserControl, IPage
{
    readonly Shell shell;
    List<string> roleNames = new();
    string tokenUrl = "";
    string? selectedId;

    public sealed record AppRow(string ClientId, string Name, string Roles, string Ttl, string Status, string LastToken, JsonNode Node);
    public sealed record SecretRow(string SecretId, string Hint, string Status, string Created, string Expires);

    public AppsPage(Shell shell)
    {
        this.shell = shell;
        InitializeComponent();
    }

    public Task ShownAsync() => LoadAsync();

    async Task LoadAsync()
    {
        var api = shell.Api;
        if (api == null) return;
        await shell.Try(async () =>
        {
            var sts = await api.GetAsync("api/v1/admin/sts");
            tokenUrl = sts.S("token_url");
            var signing = sts.Arr("keys").FirstOrDefault(k => k.S("status") == "signing");
            var next = sts.Arr("keys").FirstOrDefault(k => k.S("status") == "next");
            ModeText.Text = sts.B("require_tokens") ? "tokens required: API keys are off" : "API keys and tokens accepted";
            StsText.Text = $"token URL   {tokenUrl}\nissuer      {sts.S("issuer")} · audience {sts.S("audience")} · default token lifetime {sts.L("default_token_ttl_s") / 60} min (max {sts.L("max_token_ttl_s") / 60})\n" +
                           $"signing key {signing?.S("kid") ?? "none"} (ES256, {sts.S("key_storage")} at rest){(next != null ? $" · next key {next.S("kid")} from {Json.Local(next.S("active_from"))}" : "")}";
            var apps = await api.GetAsync("api/v1/admin/apps", new Dictionary<string, string?> { ["include_disabled"] = ShowDisabled.IsChecked == true ? "true" : null });
            var rows = apps.Arr("apps").Select(a => new AppRow(a.S("client_id"), a.S("name"), string.Join(", ", a.Arr("roles").Select(r => r.ToString())),
                $"{a.L("token_ttl_s") / 60} min", a.S("status"), a.S("last_token_at") == "" ? "never" : Json.Local(a.S("last_token_at")), a)).ToList();
            Grid.ItemsSource = rows;
            EmptyHint.Visibility = rows.Count == 0 ? Visibility.Visible : Visibility.Collapsed;
            Grid.SelectedItem = rows.FirstOrDefault(r => r.ClientId == selectedId) ?? rows.FirstOrDefault();
            if (Grid.SelectedItem == null) ShowDetail(null);
            if (roleNames.Count == 0)
                roleNames = (await api.GetAsync("api/v1/admin/access")).Arr("roles").Select(r => r.S("name")).ToList();
        });
    }

    async void Reload(object sender, RoutedEventArgs e) => await LoadAsync();

    void Grid_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (Grid.SelectedItem is AppRow row) { selectedId = row.ClientId; ShowDetail(row); }
    }

    void ShowDetail(AppRow? row)
    {
        SecretBtn.IsEnabled = DisableBtn.IsEnabled = row?.Status == "active";
        RevokeSecretBtn.IsEnabled = false;
        if (row == null)
        {
            DTitle.Text = "Select an application";
            DMeta.Text = Snippet.Text = "";
            Secrets.ItemsSource = null;
            return;
        }
        var a = row.Node;
        DTitle.Text = row.Name;
        DMeta.Text = $"client ID {row.ClientId}\nroles {row.Roles} · tokens valid {row.Ttl}\n" +
                     $"registered by {a.S("created_by")} on {Json.Local(a.S("created_at"))}" +
                     (a.S("description") != "" ? $"\n{a.S("description")}" : "") +
                     (row.Status == "disabled" ? $"\nDISABLED {Json.Local(a.S("disabled_at"))}" : "");
        Secrets.ItemsSource = a.Arr("secrets").Select(s => new SecretRow(s.S("secret_id"), s.S("hint"), s.S("status"),
            Json.Local(s.S("created_at")), s.S("expires_at") == "" ? "never" : Json.Local(s.S("expires_at")))).ToList();
        UpdateSnippet();
    }

    void Secrets_SelectionChanged(object sender, SelectionChangedEventArgs e) =>
        RevokeSecretBtn.IsEnabled = Secrets.SelectedItem is SecretRow { Status: "active" } && Grid.SelectedItem is AppRow { Status: "active" };

    void Snippet_Changed(object sender, SelectionChangedEventArgs e) { if (IsLoaded) UpdateSnippet(); }

    void UpdateSnippet()
    {
        if (Grid.SelectedItem is not AppRow row) return;
        Snippet.Text = Snippets.For(SnippetBox.SelectedIndex, shell.Api?.BaseUrl ?? "", tokenUrl, row.ClientId, "<client secret>");
    }

    void CopySnippet_Click(object sender, RoutedEventArgs e)
    {
        if (Snippet.Text != "") { Clipboard.SetText(Snippet.Text); shell.Notify("Example copied."); }
    }

    async void Register_Click(object sender, RoutedEventArgs e)
    {
        if (shell.Api == null) return;
        if (roleNames.Count == 0) await LoadAsync();
        var dlg = new RegisterAppDialog(roleNames) { Owner = Window.GetWindow(this) };
        if (dlg.ShowDialog() != true) return;
        await shell.Try(async () =>
        {
            var creds = await shell.Api.PostAsync("api/v1/admin/apps", new
            {
                name = dlg.AppName, description = dlg.Description, roles = dlg.Roles,
                token_ttl_s = dlg.TokenTtlSeconds, secret_expires_in_days = dlg.SecretDays,
            });
            selectedId = creds.S("client_id");
            ShowCredentials(creds, dlg.AppName, dlg.Roles);
            await LoadAsync();
        });
    }

    void ShowCredentials(JsonNode creds, string name, IEnumerable<string> roles) =>
        new AppCredentialsDialog(shell, name, creds.S("client_id"), creds.S("client_secret"), creds.S("token_url"), roles.ToArray())
        { Owner = Window.GetWindow(this) }.ShowDialog();

    async void NewSecret_Click(object sender, RoutedEventArgs e)
    {
        if (Grid.SelectedItem is not AppRow row || shell.Api == null) return;
        if (!Shell.Confirm($"Create a new client secret for {row.Name}?\n\nThe current secret keeps working, so you can update the application first and then revoke the old secret. An application can have two active secrets.")) return;
        await shell.Try(async () =>
        {
            var creds = await shell.Api.PostAsync($"api/v1/admin/apps/{Uri.EscapeDataString(row.ClientId)}/secrets", new { expires_in_days = 365 });
            ShowCredentials(creds, row.Name, row.Node.Arr("roles").Select(r => r.ToString()));
            await LoadAsync();
        });
    }

    async void RevokeSecret_Click(object sender, RoutedEventArgs e)
    {
        if (Grid.SelectedItem is not AppRow row || Secrets.SelectedItem is not SecretRow s || shell.Api == null) return;
        if (!Shell.Confirm($"Revoke secret {s.Hint} of {row.Name}?\n\nThe application can no longer get tokens with it. Tokens it already has stay valid until they expire (at most {row.Ttl}).")) return;
        await shell.Try(async () =>
        {
            await shell.Api.PostAsync($"api/v1/admin/apps/{Uri.EscapeDataString(row.ClientId)}/secrets/{Uri.EscapeDataString(s.SecretId)}/revoke", new { });
            await LoadAsync();
        }, $"Secret {s.Hint} revoked.");
    }

    async void Disable_Click(object sender, RoutedEventArgs e)
    {
        if (Grid.SelectedItem is not AppRow row || shell.Api == null) return;
        var reason = PromptDialog.Ask(Window.GetWindow(this), "Disable application",
            $"Disable {row.Name} ({row.ClientId})? Its tokens stop working immediately, it cannot get new ones, and runs it started fail when they next resume. This cannot be undone; register a new application instead.",
            "Reason (recorded in the audit trail)", danger: true, okText: "Disable");
        if (reason == null) return;
        await shell.Try(async () =>
        {
            await shell.Api.PostAsync($"api/v1/admin/apps/{Uri.EscapeDataString(row.ClientId)}/disable", new { reason });
            await LoadAsync();
        }, $"{row.Name} disabled.");
    }

    async void Rotate_Click(object sender, RoutedEventArgs e)
    {
        if (shell.Api == null || !Shell.Confirm("Rotate the token signing key?\n\nA new key is published now and starts signing after a short delay. Tokens signed with the current key stay valid until they expire. Every tenant uses these keys, so this needs a platform administrator.")) return;
        await shell.Try(async () => { await shell.Api.PostAsync("api/v1/admin/sts/rotate-key", new { }); await LoadAsync(); },
                        "New signing key created; it starts signing shortly.");
    }
}

/// <summary>Ready-to-run examples: get a token, then call inference with it.</summary>
public static class Snippets
{
    public static string For(int kind, string baseUrl, string tokenUrl, string clientId, string secret) => kind switch
    {
        1 => $$"""
              # 1. get an access token (client credentials)
              TOKEN=$(curl -s -u "{{clientId}}:{{secret}}" -d grant_type=client_credentials \
                "{{tokenUrl}}" | jq -r .access_token)
              # 2. call any endpoint with it
              curl -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
                -d '{"model":"local:default","input":"Hello"}' {{baseUrl}}/api/v1/inference
              """,
        2 => $"""
              from uar import Client   # pip install ./sdks/python

              # Tokens are fetched from the token service and renewed automatically.
              client = Client("{baseUrl}", client_id="{clientId}",
                              client_secret="{secret}")
              print(client.inference(model="local:default", prompt="Hello").text)
              """,
        3 => $$"""
              import { UAR } from "@uar/client";

              // Tokens are fetched from the token service and renewed automatically.
              const client = new UAR("{{baseUrl}}", {
                clientId: "{{clientId}}", clientSecret: process.env.UAR_CLIENT_SECRET });
              const r = await client.inference({ model: "local:default", prompt: "Hello" });
              console.log(r.content);
              """,
        _ => $$"""
              # 1. get an access token (client credentials)
              $t = Invoke-RestMethod -Method Post "{{tokenUrl}}" -Body @{
                grant_type = "client_credentials"; client_id = "{{clientId}}"; client_secret = "{{secret}}" }
              # 2. call any endpoint with it (valid for $($t.expires_in) seconds)
              $h = @{ Authorization = "Bearer $($t.access_token)" }
              Invoke-RestMethod -Method Post "{{baseUrl}}/api/v1/inference" -Headers $h `
                -ContentType "application/json" -Body '{"model":"local:default","input":"Hello"}'
              """,
    };
}
