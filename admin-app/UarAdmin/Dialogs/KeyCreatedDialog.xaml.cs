using System.Windows;
using UarAdmin.Services;

namespace UarAdmin.Dialogs;

public partial class KeyCreatedDialog : Window
{
    readonly Shell shell;
    readonly string key, subject;
    bool copied;

    public KeyCreatedDialog(Shell shell, string keyId, string key, string subject, string[] roles)
    {
        InitializeComponent();
        this.shell = shell;
        this.key = key;
        this.subject = subject;
        KeyBox.Text = key;
        Summary.Text = $"Key {keyId} for {subject} with role(s) {string.Join(", ", roles)}. Send it with every request as the X-API-Key header.";
        SaveProfileBtn.Visibility = roles.Contains("admin") ? Visibility.Visible : Visibility.Collapsed;
    }

    void Copy_Click(object sender, RoutedEventArgs e)
    {
        Clipboard.SetText(key);
        copied = true;
        shell.Notify("API key copied to the clipboard.", Notice.Success);
    }

    void SaveProfile_Click(object sender, RoutedEventArgs e)
    {
        var baseUrl = shell.Profile?.BaseUrl ?? "http://127.0.0.1:9000";
        var p = new ConnectionProfile { Name = $"{subject} @ {new Uri(baseUrl).Authority}", BaseUrl = baseUrl, ApiKey = key };
        shell.Settings.Profiles.RemoveAll(x => x.Name == p.Name);
        shell.Settings.Profiles.Add(p);
        shell.Save();
        copied = true;
        SaveProfileBtn.IsEnabled = false;
        shell.Notify($"Saved connection \"{p.Name}\" (key encrypted for your Windows account).", Notice.Success);
    }

    void Done_Click(object sender, RoutedEventArgs e)
    {
        if (!copied && MessageBox.Show(this, "You have not copied the key. It cannot be shown again. Close anyway?", "API key",
                MessageBoxButton.YesNo, MessageBoxImage.Warning) != MessageBoxResult.Yes) return;
        Close();
    }
}
