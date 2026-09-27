using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Threading;
using UarAdmin.Services;
using UarAdmin.Views;

namespace UarAdmin;

public partial class MainWindow : Window
{
    readonly Shell shell = new();
    readonly Dictionary<string, FrameworkElement> pages = new();
    readonly DispatcherTimer statusTimer = new() { Interval = TimeSpan.FromSeconds(5) };
    readonly DispatcherTimer toastTimer = new() { Interval = TimeSpan.FromSeconds(8) };
    IPage? current;
    bool loadingProfiles;

    public MainWindow()
    {
        InitializeComponent();
        shell.Notified += ShowToast;
        shell.ConnectionChanged += () => { LoadProfiles(); _ = RefreshStatusAsync(); };
        toastTimer.Tick += (_, _) => { Toast.Visibility = Visibility.Collapsed; toastTimer.Stop(); };
        statusTimer.Tick += async (_, _) => await RefreshStatusAsync();
        Loaded += async (_, _) =>
        {
            LoadProfiles();
            Navigate("overview");
            if (shell.Profile == null)
                shell.Notify("No connection yet. Open Settings to add the runtime URL and an admin API key.", Notice.Warning);
            await RefreshStatusAsync();
            statusTimer.Start();
            var args = Environment.GetCommandLineArgs();
            var i = Array.IndexOf(args, "--snapshot");
            if (i > 0 && i + 1 < args.Length) await SnapshotAsync(args[i + 1]);
        };
        Closing += (_, e) =>
        {
            if (shell.Runtime.OwnsProcess && !Shell.Confirm("The runtime was started by UAR Admin and stops when it closes. Close anyway?"))
                e.Cancel = true;
        };
        Closed += async (_, _) => { if (shell.Runtime.OwnsProcess) await shell.Runtime.StopAsync(shell.Api); };
    }

    void LoadProfiles()
    {
        loadingProfiles = true;
        ProfileBox.ItemsSource = null;
        ProfileBox.ItemsSource = shell.Settings.Profiles;
        ProfileBox.SelectedItem = shell.Profile;
        loadingProfiles = false;
    }

    void ProfileBox_Changed(object sender, SelectionChangedEventArgs e)
    {
        if (loadingProfiles || ProfileBox.SelectedItem is not ConnectionProfile p || p.Name == shell.Settings.ActiveProfile) return;
        shell.Settings.ActiveProfile = p.Name;
        shell.Save();
        shell.Reconnect();
        if (current != null) _ = current.ShownAsync();
    }

    void Nav_Checked(object sender, RoutedEventArgs e)
    {
        if (sender is RadioButton { Tag: string tag } && IsLoaded) Navigate(tag);
    }

    void Navigate(string tag)
    {
        if (!pages.TryGetValue(tag, out var page))
        {
            page = tag switch
            {
                "overview" => new OverviewPage(shell),
                "keys" => new KeysPage(shell),
                "api" => new ApiReferencePage(shell),
                "usage" => new UsagePage(shell),
                "approvals" => new ApprovalsPage(shell),
                "audit" => new AuditPage(shell),
                "logs" => new LogsPage(shell),
                _ => new SettingsPage(shell),
            };
            pages[tag] = page;
        }
        current?.Hidden();
        Host.Content = page;
        current = page as IPage;
        _ = current?.ShownAsync();
    }

    async Task RefreshStatusAsync()
    {
        var api = shell.Api;
        if (api == null) { SetStatus("#64748B", "no connection", ""); return; }
        if (!await api.HealthyAsync()) { SetStatus("#DC2626", $"offline · {api.BaseUrl}", ""); PendingBadge.Visibility = Visibility.Collapsed; return; }
        try
        {
            var info = await api.GetAsync("api/v1/admin/info");
            SetStatus("#22C55E", $"online · v{info.S("version")}", $"{info.S("subject")} · tenant {info.S("tenant")}");
        }
        catch (ApiException e) when (e.Status is 401 or 403)
        {
            SetStatus("#F59E0B", "online · key not admin", e.Status == 401 ? "API key rejected" : "needs an admin key");
        }
        catch (ApiException) { SetStatus("#F59E0B", "online · degraded", ""); }
        try
        {
            var pending = (await api.GetAsync("api/v1/approvals", new Dictionary<string, string?> { ["status"] = "pending" })).Arr("approvals").Count();
            PendingBadge.Visibility = pending > 0 ? Visibility.Visible : Visibility.Collapsed;
            PendingText.Text = pending.ToString();
        }
        catch (ApiException) { PendingBadge.Visibility = Visibility.Collapsed; }
    }

    void SetStatus(string color, string text, string identity)
    {
        StatusDot.Fill = new SolidColorBrush((Color)ColorConverter.ConvertFromString(color));
        StatusText.Text = text;
        IdentityText.Text = identity;
    }

    void ShowToast(string message, Notice kind)
    {
        Dispatcher.Invoke(() =>
        {
            var (bg, fg) = kind switch
            {
                Notice.Success => ("SuccessSoft", "Success"),
                Notice.Warning => ("WarningSoft", "Warning"),
                Notice.Error => ("DangerSoft", "Danger"),
                _ => ("AccentSoft", "Accent"),
            };
            Toast.Background = (Brush)FindResource(bg);
            ToastText.Foreground = (Brush)FindResource(fg);
            ToastText.Text = message;
            Toast.Visibility = Visibility.Visible;
            toastTimer.Stop();
            toastTimer.Interval = TimeSpan.FromSeconds(kind == Notice.Error ? 15 : 7);
            toastTimer.Start();
        });
    }

    void Toast_Close(object sender, RoutedEventArgs e) => Toast.Visibility = Visibility.Collapsed;

    /// <summary>Diagnostics: UarAdmin.exe --snapshot DIR renders every page to DIR\page.png, then exits.</summary>
    async Task SnapshotAsync(string dir)
    {
        System.IO.Directory.CreateDirectory(dir);
        foreach (var rb in Nav.Children.OfType<RadioButton>())
        {
            rb.IsChecked = true;
            await Task.Delay(3500);
            var dpi = VisualTreeHelper.GetDpi(this);
            var root = (FrameworkElement)Content;
            var bmp = new System.Windows.Media.Imaging.RenderTargetBitmap((int)(root.ActualWidth * dpi.DpiScaleX), (int)(root.ActualHeight * dpi.DpiScaleY),
                                                                          dpi.PixelsPerInchX, dpi.PixelsPerInchY, PixelFormats.Pbgra32);
            bmp.Render(root);
            var enc = new System.Windows.Media.Imaging.PngBitmapEncoder();
            enc.Frames.Add(System.Windows.Media.Imaging.BitmapFrame.Create(bmp));
            using var f = System.IO.File.Create(System.IO.Path.Combine(dir, $"{rb.Tag}.png"));
            enc.Save(f);
        }
        Application.Current.Shutdown();
    }
}
