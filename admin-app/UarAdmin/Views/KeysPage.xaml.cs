using System.Text;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using UarAdmin.Dialogs;
using UarAdmin.Services;

namespace UarAdmin.Views;

public partial class KeysPage : UserControl, IPage
{
    readonly Shell shell;
    List<string> roleNames = new();

    public sealed record KeyRow(string KeyId, string Subject, string Roles, string Status, string Source, string Created,
                                string Expires, string CreatedBy, string Description)
    {
        public Brush StatusBg => new SolidColorBrush(Status == "active" ? Color.FromRgb(0xDC, 0xFC, 0xE7) : Color.FromRgb(0xFE, 0xE2, 0xE2));
        public Brush StatusFg => new SolidColorBrush(Status == "active" ? Color.FromRgb(0x16, 0x65, 0x34) : Color.FromRgb(0x99, 0x1B, 0x1B));
    }

    public sealed record RoleRow(string Name, string Permissions);

    public KeysPage(Shell shell)
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
            var keys = await api.GetAsync("api/v1/admin/keys", new Dictionary<string, string?>
            { ["include_revoked"] = ShowRevoked.IsChecked == true ? "true" : null });
            Grid.ItemsSource = keys.Arr("keys").Select(k => new KeyRow(
                k.S("key_id"), k.S("subject"), string.Join(", ", k.Arr("roles").Select(r => r.ToString())), k.S("status"),
                k.S("source"), Json.Local(k.S("created_at")), k.S("expires_at") == "" ? "never" : Json.Local(k.S("expires_at")),
                k.S("created_by"), k.S("description"))).ToList();
            var pol = await api.GetAsync("api/v1/admin/access");
            var roles = pol.Arr("roles").Select(r => new RoleRow(r.S("name"), string.Join(", ", r.Arr("permissions").Select(x => x.ToString())))).ToList();
            RolesGrid.ItemsSource = roles;
            roleNames = roles.Select(r => r.Name).ToList();
            var sb = new StringBuilder();
            foreach (var o in pol.Arr("oidc"))
            {
                sb.AppendLine($"{o.S("issuer")}  (audience {o.S("audience")}, {o.S("keys")})");
                foreach (var m in o.Arr("mappings"))
                    sb.AppendLine($"  {m.S("kind"),-8} {m.S("source"),-28} -> {string.Join(", ", m.Arr("roles").Select(x => x.ToString()))}");
                sb.AppendLine();
            }
            OidcText.Text = sb.Length > 0 ? sb.ToString() : "No OIDC issuer is configured for this tenant.\nUsers sign in with API keys only.";
        });
    }

    async void Reload(object sender, RoutedEventArgs e) => await LoadAsync();

    void Grid_SelectionChanged(object sender, SelectionChangedEventArgs e) =>
        RevokeBtn.IsEnabled = Grid.SelectedItem is KeyRow { Source: "api", Status: "active" };

    async void New_Click(object sender, RoutedEventArgs e)
    {
        if (shell.Api == null) return;
        if (roleNames.Count == 0) await LoadAsync();
        var dlg = new CreateKeyDialog(roleNames) { Owner = Window.GetWindow(this) };
        if (dlg.ShowDialog() != true) return;
        await shell.Try(async () =>
        {
            var created = await shell.Api.PostAsync("api/v1/admin/keys", new
            {
                subject = dlg.Subject, roles = dlg.Roles, description = dlg.Description, expires_in_days = dlg.ExpiresInDays,
            });
            new KeyCreatedDialog(shell, created["key"].S("key_id"), created.S("api_key"), dlg.Subject, dlg.Roles)
            { Owner = Window.GetWindow(this) }.ShowDialog();
            await LoadAsync();
        });
    }

    async void Revoke_Click(object sender, RoutedEventArgs e)
    {
        if (Grid.SelectedItem is not KeyRow row || shell.Api == null) return;
        var reason = PromptDialog.Ask(Window.GetWindow(this), "Revoke API key",
            $"Revoke key {row.KeyId} ({row.Subject})? Applications using it stop working immediately, and runs it started fail when they next resume.",
            "Reason (recorded in the audit trail)", danger: true, okText: "Revoke");
        if (reason == null) return;
        await shell.Try(async () =>
        {
            await shell.Api.PostAsync($"api/v1/admin/keys/{Uri.EscapeDataString(row.KeyId)}/revoke", new { reason });
            await LoadAsync();
        }, $"Key {row.KeyId} revoked.");
    }

    void CopyId_Click(object sender, RoutedEventArgs e)
    {
        if (Grid.SelectedItem is KeyRow row) { Clipboard.SetText(row.KeyId); shell.Notify($"Copied {row.KeyId}."); }
    }
}
