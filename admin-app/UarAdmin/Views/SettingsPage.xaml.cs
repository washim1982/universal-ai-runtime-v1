using System.IO;
using System.Windows;
using System.Windows.Controls;
using Microsoft.Win32;
using UarAdmin.Services;

namespace UarAdmin.Views;

public partial class SettingsPage : UserControl, IPage
{
    readonly Shell shell;

    public SettingsPage(Shell shell)
    {
        this.shell = shell;
        InitializeComponent();
    }

    public Task ShownAsync()
    {
        LoadProfiles(shell.Profile);
        RootBox.Text = shell.Settings.RuntimeRoot;
        ConfigBox.Text = shell.Settings.ConfigPath;
        RefreshBox.Text = shell.Settings.RefreshSeconds.ToString();
        RootHint.Text = SettingsStore.IsRuntimeRoot(shell.Settings.RuntimeRoot) ? "  ✔ runtime folder found" : "  runtime folder not found";
        AboutText.Text = $"UAR Admin {typeof(SettingsPage).Assembly.GetName().Version?.ToString(3)} · settings: {SettingsStore.Folder} · runtime logs: {SettingsStore.LogFolder}";
        return Task.CompletedTask;
    }

    void LoadProfiles(ConnectionProfile? select)
    {
        Profiles.ItemsSource = null;
        Profiles.ItemsSource = shell.Settings.Profiles;
        Profiles.SelectedItem = select;
        if (select == null) ClearForm();
    }

    void ClearForm()
    {
        PName.Text = "";
        PUrl.Text = $"http://127.0.0.1:{SettingsStore.LocalHttpPort(shell.Settings)}";
        PKey.Password = "";
        TestText.Text = "";
    }

    void Profiles_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (Profiles.SelectedItem is not ConnectionProfile p) return;
        PName.Text = p.Name;
        PUrl.Text = p.BaseUrl;
        PKey.Password = "";
        TestText.Text = p.Name == shell.Settings.ActiveProfile ? "This is the active connection." : "";
    }

    void NewProfile_Click(object sender, RoutedEventArgs e) { Profiles.SelectedItem = null; ClearForm(); PName.Focus(); }

    bool ValidUrl(out string url)
    {
        url = PUrl.Text.Trim().TrimEnd('/');
        return Uri.TryCreate(url, UriKind.Absolute, out var u) && (u.Scheme == "http" || u.Scheme == "https");
    }

    void SaveProfile_Click(object sender, RoutedEventArgs e)
    {
        var name = PName.Text.Trim();
        if (name == "") { TestText.Text = "Enter a name."; return; }
        if (!ValidUrl(out var url)) { TestText.Text = "Enter a valid http(s) URL, e.g. http://127.0.0.1:9000"; return; }
        var existing = Profiles.SelectedItem as ConnectionProfile;
        if (existing == null && PKey.Password == "") { TestText.Text = "Enter the API key."; return; }
        if (existing != null && existing.Name != name && shell.Settings.Profiles.Any(p => p.Name == name)) { TestText.Text = "A connection with this name exists."; return; }
        var p = existing ?? new ConnectionProfile();
        var wasActive = existing != null && existing.Name == shell.Settings.ActiveProfile;
        p.Name = name;
        p.BaseUrl = url;
        if (PKey.Password != "") p.ApiKey = PKey.Password.Trim();
        if (existing == null) shell.Settings.Profiles.Add(p);
        if (wasActive || shell.Settings.Profiles.Count == 1) shell.Settings.ActiveProfile = p.Name;
        shell.Save();
        shell.Reconnect();
        LoadProfiles(p);
        TestText.Text = "Saved.";
    }

    void UseProfile_Click(object sender, RoutedEventArgs e)
    {
        if (Profiles.SelectedItem is not ConnectionProfile p) return;
        shell.Settings.ActiveProfile = p.Name;
        shell.Save();
        shell.Reconnect();
        TestText.Text = $"Now using \"{p.Name}\".";
    }

    void DeleteProfile_Click(object sender, RoutedEventArgs e)
    {
        if (Profiles.SelectedItem is not ConnectionProfile p || !Shell.Confirm($"Delete the connection \"{p.Name}\"? The saved key is removed from this PC.")) return;
        shell.Settings.Profiles.Remove(p);
        if (shell.Settings.ActiveProfile == p.Name) shell.Settings.ActiveProfile = shell.Settings.Profiles.FirstOrDefault()?.Name ?? "";
        shell.Save();
        shell.Reconnect();
        LoadProfiles(null);
    }

    void Import_Click(object sender, RoutedEventArgs e)
    {
        if (!SettingsStore.ImportLocalProfile(shell.Settings))
        {
            TestText.Text = "No UAR_ADMIN_KEY found in .local\\credentials.env of the runtime folder. Run the setup dashboard's configuration step first.";
            return;
        }
        shell.Save();
        shell.Reconnect();
        LoadProfiles(shell.Profile);
        TestText.Text = "Imported the local admin key and made it the active connection.";
    }

    async void Test_Click(object sender, RoutedEventArgs e)
    {
        if (!ValidUrl(out var url)) { TestText.Text = "Enter a valid URL."; return; }
        var key = PKey.Password != "" ? PKey.Password.Trim() : (Profiles.SelectedItem as ConnectionProfile)?.ApiKey ?? "";
        using var api = new ApiClient(url, key);
        TestText.Text = "Testing…";
        if (!await api.HealthyAsync()) { TestText.Text = $"✖ {url} is not reachable (is the runtime running?)."; return; }
        try
        {
            var info = await api.GetAsync("api/v1/admin/info");
            TestText.Text = $"✔ Connected to runtime {info.S("version")} as {info.S("subject")} (tenant {info.S("tenant")}){(info.B("platform_admin") ? ", platform administrator" : "")}.";
        }
        catch (ApiException ex)
        {
            TestText.Text = ex.Status == 401 ? "✖ The runtime rejected this API key." :
                            ex.Status == 403 ? "⚠ The key works but is not an admin key: most pages will be read-only or denied." : $"✖ {ex.Message}";
        }
    }

    void Browse_Click(object sender, RoutedEventArgs e)
    {
        var dlg = new OpenFolderDialog { Title = "Select the Universal AI Runtime folder", InitialDirectory = Directory.Exists(RootBox.Text) ? RootBox.Text : "" };
        if (dlg.ShowDialog() == true) RootBox.Text = dlg.FolderName;
    }

    void SaveRuntime_Click(object sender, RoutedEventArgs e)
    {
        var root = RootBox.Text.Trim();
        if (root != "" && !SettingsStore.IsRuntimeRoot(root)) { RootHint.Text = "  ✖ that folder does not contain runtime\\uar_runtime and proto"; return; }
        if (!int.TryParse(RefreshBox.Text, out var secs) || secs < 2 || secs > 300) { RootHint.Text = "  ✖ refresh interval: 2-300 seconds"; return; }
        shell.Settings.RuntimeRoot = root;
        shell.Settings.ConfigPath = ConfigBox.Text.Trim() == "" ? @".local\uar.yaml" : ConfigBox.Text.Trim();
        shell.Settings.RefreshSeconds = secs;
        shell.Save();
        RootHint.Text = "  ✔ saved";
    }
}
