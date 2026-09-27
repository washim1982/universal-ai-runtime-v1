using System.Collections.ObjectModel;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Threading;
using Microsoft.Win32;
using UarAdmin.Services;

namespace UarAdmin.Views;

public partial class LogsPage : UserControl, IPage
{
    const int MaxRows = 10_000;
    static readonly string[] Levels = { "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL" };
    readonly Shell shell;
    readonly DispatcherTimer timer = new() { Interval = TimeSpan.FromSeconds(2) };
    readonly List<LogRow> all = new();
    readonly ObservableCollection<LogRow> view = new();
    long afterSeq;
    bool polling;
    int dockerTicks;

    public sealed record LogRow(string Time, string Level, string Logger, string Message, string Detail)
    {
        public Brush LevelBrush => new SolidColorBrush(Level switch
        {
            "ERROR" or "CRITICAL" => Color.FromRgb(0xDC, 0x26, 0x26),
            "WARNING" => Color.FromRgb(0xD9, 0x77, 0x06),
            "DEBUG" => Color.FromRgb(0x94, 0xA3, 0xB8),
            _ => Color.FromRgb(0x25, 0x63, 0xEB),
        });
    }

    public LogsPage(Shell shell)
    {
        this.shell = shell;
        InitializeComponent();
        Grid.ItemsSource = view;
        timer.Tick += async (_, _) => await PollAsync();
        shell.Runtime.OutputLine += line => Dispatcher.BeginInvoke(() => { if (Source == "process") Add(ParseLine(line)); });
        shell.ConnectionChanged += () => { afterSeq = 0; if (Source == "api") ClearRows(); };
    }

    string Source => (string)((ComboBoxItem)SourceBox.SelectedItem).Tag;
    int MinLevel => Math.Max(0, Array.IndexOf(Levels, (string)((ComboBoxItem)LevelBox.SelectedItem).Content));

    public async Task ShownAsync()
    {
        timer.Start();
        await ReloadAsync();
    }

    public void Hidden() => timer.Stop();

    async Task ReloadAsync()
    {
        ClearRows();
        afterSeq = 0;
        switch (Source)
        {
            case "process":
                foreach (var line in shell.Runtime.OutputSnapshot()) Add(ParseLine(line), refresh: false);
                RefreshView();
                StatusText.Text = shell.Runtime.OwnsProcess || all.Count > 0
                    ? $"Console output of the runtime started by UAR Admin · saved to {SettingsStore.LogFolder}"
                    : "No runtime has been started by UAR Admin in this session. Use Overview → Start, or choose another source.";
                break;
            case "docker":
                await shell.Runtime.RefreshAsync(shell.Api);
                var lines = await shell.Runtime.DockerLogsAsync(1000);
                foreach (var line in lines.Where(l => l.Trim() != "")) Add(ParseLine(line), refresh: false);
                RefreshView();
                StatusText.Text = shell.Runtime.DockerContainer != null
                    ? $"Last 1000 lines of {shell.Runtime.DockerContainer} (refreshes every few seconds)"
                    : "No running Docker runtime container was found.";
                break;
            default:
                await PollAsync();
                break;
        }
    }

    async Task PollAsync()
    {
        if (polling || PauseBtn.IsChecked == true) return;
        polling = true;
        try
        {
            if (Source == "docker")
            {
                if (++dockerTicks % 3 == 0) await ReloadDockerQuietAsync();   // every ~6 s
                return;
            }
            if (Source != "api" || shell.Api == null) return;
            var page = await shell.Api.GetAsync("api/v1/admin/logs", new Dictionary<string, string?>
            { ["after_seq"] = afterSeq.ToString(), ["limit"] = "2000" });
            foreach (var r in page.Arr("records"))
            {
                var fields = r["fields"] is JsonObject o && o.Count > 0 ? Json.Pretty(o) : "";
                Add(new LogRow(Json.Local(r.S("ts")), r.S("level"), r.S("logger"), r.S("message"), fields), refresh: false);
            }
            afterSeq = Math.Max(afterSeq, page.L("next_after_seq"));
            RefreshView();
            StatusText.Text = $"Live from {shell.Api.BaseUrl} · {all.Count:N0} records (the runtime keeps the last {page.L("capacity"):N0}) · updated {DateTime.Now:HH:mm:ss}";
        }
        catch (ApiException e)
        {
            StatusText.Text = e.Status == 403 ? $"Not allowed: {e.Message}" : $"Runtime unreachable ({e.Message}). If UAR Admin started it, see Process output.";
        }
        finally { polling = false; }
    }

    async Task ReloadDockerQuietAsync()
    {
        var lines = await shell.Runtime.DockerLogsAsync(1000);
        if (lines.Length == 0) return;
        ClearRows();
        foreach (var line in lines.Where(l => l.Trim() != "")) Add(ParseLine(line), refresh: false);
        RefreshView();
    }

    /// <summary>Console lines are JSON log records (observability.log_json) or plain text.</summary>
    static LogRow ParseLine(string line)
    {
        var t = line.Trim();
        if (t.StartsWith('{'))
        {
            try
            {
                var n = JsonNode.Parse(t)!.AsObject();
                var rest = new JsonObject();
                foreach (var (k, v) in n)
                    if (k is not ("ts" or "level" or "logger" or "msg")) rest[k] = v?.DeepClone();
                return new LogRow(Json.Local(n.S("ts") + (n.S("ts").EndsWith("Z") ? "" : "Z")), n.S("level", "INFO"), n.S("logger"), n.S("msg"),
                                  rest.Count > 0 ? Json.Pretty(rest) : "");
            }
            catch (JsonException) { }
        }
        var level = Levels.FirstOrDefault(l => t.Contains($" {l} ") || t.StartsWith(l + ":")) ??
                    (t.Contains("Traceback") || t.Contains("Error") ? "ERROR" : "INFO");
        return new LogRow(DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss"), level, "console", t, "");
    }

    void Add(LogRow row, bool refresh = true)
    {
        all.Add(row);
        if (all.Count > MaxRows) all.RemoveRange(0, all.Count - MaxRows);
        if (refresh && Matches(row))
        {
            view.Add(row);
            if (view.Count > MaxRows) view.RemoveAt(0);
            ScrollToEnd();
        }
    }

    bool Matches(LogRow r)
    {
        var q = SearchBox.Text.Trim();
        return Math.Max(0, Array.IndexOf(Levels, r.Level)) >= MinLevel &&
               (q == "" || $"{r.Logger} {r.Message} {r.Detail}".Contains(q, StringComparison.OrdinalIgnoreCase));
    }

    void RefreshView()
    {
        var atEnd = view.Count == 0 || Grid.SelectedIndex < 0;
        view.Clear();
        foreach (var r in all.Where(Matches)) view.Add(r);
        if (atEnd) ScrollToEnd();
    }

    void ScrollToEnd()
    {
        if (view.Count > 0 && Grid.SelectedIndex < 0) Grid.ScrollIntoView(view[^1]);
    }

    void ClearRows() { all.Clear(); view.Clear(); Detail.Text = ""; }

    void Grid_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (Grid.SelectedItem is LogRow r) Detail.Text = $"{r.Time}  {r.Level}  {r.Logger}\n{r.Message}\n{r.Detail}";
    }

    async void Source_Changed(object sender, SelectionChangedEventArgs e) { if (IsLoaded) await ReloadAsync(); }
    void Level_Changed(object sender, SelectionChangedEventArgs e) { if (IsLoaded) RefreshView(); }
    void Search_Changed(object sender, TextChangedEventArgs e) => RefreshView();
    void Clear_Click(object sender, RoutedEventArgs e) => ClearRows();

    void Save_Click(object sender, RoutedEventArgs e)
    {
        var dlg = new SaveFileDialog { FileName = $"uar-logs-{DateTime.Now:yyyyMMdd-HHmm}.log", Filter = "Log|*.log|Text|*.txt" };
        if (dlg.ShowDialog() != true) return;
        File.WriteAllLines(dlg.FileName, view.Select(r => $"{r.Time} {r.Level,-8} {r.Logger} {r.Message}{(r.Detail != "" ? " " + r.Detail.Replace("\n", " ") : "")}"), Encoding.UTF8);
        shell.Notify($"Saved {view.Count:N0} lines to {dlg.FileName}.", Notice.Success);
    }

    void OpenFolder_Click(object sender, RoutedEventArgs e)
    {
        Directory.CreateDirectory(SettingsStore.LogFolder);
        Process.Start(new ProcessStartInfo("explorer.exe", SettingsStore.LogFolder) { UseShellExecute = true });
    }
}
