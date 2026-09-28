using System.Text;
using System.Text.Json.Nodes;
using System.Windows;
using System.Windows.Controls;
using UarAdmin.Services;
using UarAdmin.Views;

namespace UarAdmin.Dialogs;

public partial class AppCredentialsDialog : Window
{
    readonly Shell shell;
    readonly string name, clientId, secret, tokenUrl;
    bool copied;

    public AppCredentialsDialog(Shell shell, string name, string clientId, string secret, string tokenUrl, string[] roles)
    {
        InitializeComponent();
        this.shell = shell;
        (this.name, this.clientId, this.secret, this.tokenUrl) = (name, clientId, secret, tokenUrl);
        Title1.Text = $"{name}: credentials";
        IdBox.Text = clientId;
        SecretBox.Text = secret;
        UrlBox.Text = tokenUrl;
        UseBtn.Visibility = roles.Contains("admin") ? Visibility.Visible : Visibility.Collapsed;
        Loaded += (_, _) => UpdateSnippet();
    }

    void UpdateSnippet() => Snippet.Text = Snippets.For(SnippetBox.SelectedIndex, shell.Api?.BaseUrl ?? "", tokenUrl, clientId, secret);
    void Snippet_Changed(object sender, SelectionChangedEventArgs e) { if (IsLoaded) UpdateSnippet(); }

    void Copy(string text, string what) { Clipboard.SetText(text); shell.Notify($"{what} copied."); }
    void CopyId_Click(object sender, RoutedEventArgs e) => Copy(clientId, "Client ID");
    void CopyUrl_Click(object sender, RoutedEventArgs e) => Copy(tokenUrl, "Token URL");
    void CopySecret_Click(object sender, RoutedEventArgs e) { Copy(secret, "Client secret"); copied = true; }
    void CopySnippet_Click(object sender, RoutedEventArgs e) { Copy(Snippet.Text, "Example (contains the secret)"); copied = true; }

    /// <summary>Exchange the credentials at the token service, show the token's claims, and call the API with it.</summary>
    async void Test_Click(object sender, RoutedEventArgs e)
    {
        if (shell.Api == null) return;
        TestBtn.IsEnabled = false;
        TestPanel.Visibility = Visibility.Visible;
        TestText.Text = "Requesting a token…";
        try
        {
            using var app = new ApiClient(shell.Api.BaseUrl, clientId, secret);
            var t = await app.RequestTokenAsync(clientId, secret, null);
            var claims = Claims(t.AccessToken);
            var sb = new StringBuilder();
            sb.AppendLine($"✔ token issued: Bearer, expires in {t.ExpiresIn / 60} min, scope \"{t.Scope}\"");
            sb.AppendLine($"  sub {claims.S("sub")} · tenant {claims.S("uar_tenant")} · roles [{string.Join(", ", claims.Arr("roles").Select(r => r.ToString()))}]");
            sb.AppendLine($"  iss {claims.S("iss")} · aud {claims.S("aud")} · jti {claims.S("jti")}");
            try
            {
                var models = await app.GetAsync("api/v1/models");
                sb.Append($"✔ GET /api/v1/models with the token: {models.Arr("models").Count()} models");
            }
            catch (ApiException ex) { sb.Append($"✖ GET /api/v1/models with the token: {ex.Message} ({ex.Status})"); }
            TestText.Text = sb.ToString();
        }
        catch (ApiException ex) { TestText.Text = $"✖ token request failed: {ex.Code}: {ex.Message}"; }
        finally { TestBtn.IsEnabled = true; }
    }

    static JsonNode Claims(string jwt)
    {
        var part = jwt.Split('.')[1].Replace('-', '+').Replace('_', '/');
        part = part.PadRight(part.Length + (4 - part.Length % 4) % 4, '=');
        return JsonNode.Parse(Encoding.UTF8.GetString(Convert.FromBase64String(part)))!;
    }

    void Use_Click(object sender, RoutedEventArgs e)
    {
        var baseUrl = shell.Api?.BaseUrl ?? "http://127.0.0.1:9000";
        var p = new ConnectionProfile { Name = $"{name} (app) @ {new Uri(baseUrl).Authority}", BaseUrl = baseUrl, AuthType = "app", ClientId = clientId };
        p.ClientSecret = secret;
        shell.Settings.Profiles.RemoveAll(x => x.Name == p.Name);
        shell.Settings.Profiles.Add(p);
        shell.Save();
        copied = true;
        UseBtn.IsEnabled = false;
        shell.Notify($"Saved connection \"{p.Name}\". Choose it under CONNECTION to sign in with this application.", Notice.Success);
    }

    void Done_Click(object sender, RoutedEventArgs e)
    {
        if (!copied && MessageBox.Show(this, "You have not copied the client secret. It cannot be shown again. Close anyway?",
                "Application credentials", MessageBoxButton.YesNo, MessageBoxImage.Warning) != MessageBoxResult.Yes) return;
        Close();
    }
}
