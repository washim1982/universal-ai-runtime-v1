using System.Diagnostics;
using System.IO;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Threading;
using UarAdmin.Services;

namespace UarAdmin.Views;

public partial class OverviewPage : UserControl, IPage
{
    readonly Shell shell;
    readonly DispatcherTimer timer = new();
    bool busy;

    public sealed record ComponentRow(string Name, string Kind, string Status, bool Ok)
    {
        public Brush Dot => B(Ok ? "#22C55E" : "#F59E0B");
    }

    public OverviewPage(Shell shell)
    {
        this.shell = shell;
        InitializeComponent();
        timer.Tick += async (_, _) => { if (!busy) await RefreshAsync(); };
    }

    public async Task ShownAsync()
    {
        timer.Interval = TimeSpan.FromSeconds(Math.Max(2, shell.Settings.RefreshSeconds));
        timer.Start();
        await RefreshAsync();
    }

    public void Hidden() => timer.Stop();

    static Brush B(string hex) => new SolidColorBrush((Color)ColorConverter.ConvertFromString(hex));

    async Task RefreshAsync()
    {
        var api = shell.Api;
        UrlText.Text = api?.BaseUrl ?? "(no connection - see Settings)";
        CommandText.Text = shell.Settings.RuntimeRoot != "" ? $"local start: {shell.Runtime.CommandLine}" : "Runtime folder not set (Settings).";
        await shell.Runtime.RefreshAsync(api);
        var rt = shell.Runtime;
        var (label, bg, fg) = rt.Mode switch
        {
            RuntimeMode.Managed => ("Running", "#DCFCE7", "#166534"),
            RuntimeMode.External => ("Running", "#DCFCE7", "#166534"),
            RuntimeMode.Docker => ("Running (Docker)", "#DCFCE7", "#166534"),
            RuntimeMode.Starting => ("Starting…", "#FEF3C7", "#92400E"),
            _ => ("Stopped", "#FEE2E2", "#991B1B"),
        };
        StateText.Text = label;
        StatePill.Background = B(bg);
        StateText.Foreground = B(fg);
        ModeText.Text = rt.Mode switch
        {
            RuntimeMode.Managed => $"Managed by UAR Admin (pid {rt.Pid}) - output in Service logs",
            RuntimeMode.External => rt.Detail,
            RuntimeMode.Docker => $"Docker: {rt.Detail}",
            RuntimeMode.Starting => "Starting…",
            _ => "Not running",
        };
        var running = rt.Mode is RuntimeMode.Managed or RuntimeMode.External or RuntimeMode.Docker;
        StartBtn.IsEnabled = !running && rt.Mode != RuntimeMode.Starting;
        StopBtn.IsEnabled = running;
        RestartBtn.IsEnabled = running;

        if (api == null || !running)
        {
            VersionText.Text = UptimeText.Text = HostText.Text = MigrationsText.Text = "–";
            Components.ItemsSource = null;
            ComponentsHint.Text = running ? "" : "Start the runtime to see its components.";
            return;
        }
        try
        {
            var info = await api.GetAsync("api/v1/admin/info");
            VersionText.Text = $"{info.S("version")} · {info.S("profile")}";
            var up = TimeSpan.FromSeconds(info.L("uptime_s"));
            UptimeText.Text = $"{Json.Local(info.S("started_at"))}  ({(int)up.TotalHours}h {up.Minutes}m)";
            HostText.Text = info.S("host") != "" ? $"{info.S("host")} · pid {info.S("pid")} · worker {(info.B("worker") ? "running" : "off")}" : "–";
            MigrationsText.Text = string.Join(", ", info.Arr("migrations").Select(m => m.ToString()));
            WhoText.Text = info.S("subject");
            TenantText.Text = $"tenant {info.S("tenant")}" + (info.B("platform_admin") ? " · platform administrator" : "");
            var rows = info.Arr("components").Select(c => new ComponentRow(c.S("name"), c.S("kind"), c.S("status"), c.B("ok"))).ToList();
            Components.ItemsSource = rows;
            ComponentsHint.Text = rows.Count == 0 ? "Component details are shown to platform administrators only." :
                $"{rows.Count(r => r.Ok)} of {rows.Count} healthy";
        }
        catch (ApiException e)
        {
            VersionText.Text = UptimeText.Text = HostText.Text = MigrationsText.Text = "–";
            ComponentsHint.Text = e.Status is 401 or 403 ? "This connection's key is not an admin key." : e.Message;
            Components.ItemsSource = null;
        }
        try
        {
            var since = DateTimeOffset.UtcNow.AddDays(-1).ToString("yyyy-MM-ddTHH:mm:ssZ");
            var u = await api.GetAsync("api/v1/usage", new Dictionary<string, string?> { ["since"] = since, ["limit"] = "1" });
            CallsNum.Text = u.L("total_requests").ToString("N0");
            TokensNum.Text = (u.L("total_input_tokens") + u.L("total_output_tokens")).ToString("N0");
            CostNum.Text = $"{u?["total_cost"].S("amount", "0")} {u?["total_cost"].S("currency")}";
        }
        catch (ApiException) { CallsNum.Text = TokensNum.Text = CostNum.Text = "–"; }
        try
        {
            PendingNum.Text = (await api.GetAsync("api/v1/approvals", new Dictionary<string, string?> { ["status"] = "pending" }))
                .Arr("approvals").Count().ToString();
        }
        catch (ApiException) { PendingNum.Text = "–"; }
    }

    async Task Run(Func<Task> action, string done)
    {
        busy = true;
        StartBtn.IsEnabled = StopBtn.IsEnabled = RestartBtn.IsEnabled = false;
        StateText.Text = "Working…";
        try { await shell.Try(action, done); }
        finally { busy = false; await RefreshAsync(); }
    }

    async void Start_Click(object sender, RoutedEventArgs e) =>
        await Run(() => shell.Runtime.StartAsync(shell.Api), "Runtime started.");

    async void Stop_Click(object sender, RoutedEventArgs e)
    {
        var rt = shell.Runtime;
        var what = rt.Mode switch
        {
            RuntimeMode.External => $"The runtime was not started by UAR Admin ({rt.Detail}). Stop that process?",
            RuntimeMode.Docker => $"Stop the Docker container {rt.DockerContainer}?",
            _ => "Stop the runtime? Running agent runs are resumed by a worker when it starts again.",
        };
        if (!Shell.Confirm(what)) return;
        await Run(() => shell.Runtime.StopAsync(shell.Api), "Runtime stopped.");
    }

    async void Restart_Click(object sender, RoutedEventArgs e)
    {
        if (shell.Runtime.Mode == RuntimeMode.External &&
            !Shell.Confirm("The runtime was started outside UAR Admin. Restart it here (it will then be managed by UAR Admin)?")) return;
        await Run(() => shell.Runtime.RestartAsync(shell.Api), "Runtime restarted.");
    }

    async void Refresh_Click(object sender, RoutedEventArgs e) => await RefreshAsync();

    void OpenFolder_Click(object sender, RoutedEventArgs e)
    {
        if (Directory.Exists(shell.Settings.RuntimeRoot))
            Process.Start(new ProcessStartInfo("explorer.exe", shell.Settings.RuntimeRoot) { UseShellExecute = true });
        else shell.Notify("Runtime folder not set - see Settings.", Notice.Warning);
    }
}
