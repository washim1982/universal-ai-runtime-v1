using System.IO;
using System.Text;
using System.Text.Json.Nodes;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using Microsoft.Win32;
using UarAdmin.Services;

namespace UarAdmin.Views;

public partial class AuditPage : UserControl, IPage
{
    const int MaxRecords = 50_000;
    readonly Shell shell;
    List<AuditRow> all = new();
    bool loaded;

    public sealed record AuditRow(long Seq, string Time, string Actor, string Action, string Target, string Outcome, string RunId, JsonNode Node);

    public AuditPage(Shell shell)
    {
        this.shell = shell;
        InitializeComponent();
    }

    public async Task ShownAsync()
    {
        if (!loaded) await LoadAsync();
    }

    /// <summary>The export API pages forward from the oldest entry; the grid shows newest first.</summary>
    async Task LoadAsync()
    {
        if (shell.Api == null) return;
        await shell.Try(async () =>
        {
            var rows = new List<AuditRow>();
            long after = 0;
            while (rows.Count < MaxRecords)
            {
                var page = await shell.Api.GetAsync("api/v1/audit/export", new Dictionary<string, string?>
                { ["after_seq"] = after.ToString(), ["limit"] = "1000" });
                var recs = page.Arr("records").ToList();
                if (recs.Count == 0) break;
                rows.AddRange(recs.Select(r => new AuditRow(r.L("seq"), Json.Local(r.S("ts")), r.S("actor"), r.S("action"),
                    r.S("target"), r.S("outcome"), r.S("run_id"), r)));
                after = page.L("next_after_seq");
                if (recs.Count < 1000) break;
            }
            rows.Reverse();
            all = rows;
            loaded = true;
            ApplyFilter();
        });
    }

    void ApplyFilter()
    {
        var q = FilterBox.Text.Trim();
        var view = string.IsNullOrEmpty(q) ? all : all.Where(r =>
            $"{r.Actor} {r.Action} {r.Target} {r.Outcome} {r.RunId}".Contains(q, StringComparison.OrdinalIgnoreCase)).ToList();
        Grid.ItemsSource = view;
        if (view.Count > 0) Grid.SelectedIndex = 0;
        CountText.Text = $"{view.Count:N0} of {all.Count:N0} entries{(all.Count >= MaxRecords ? $" (first {MaxRecords:N0} loaded)" : "")}";
    }

    void Filter_Changed(object sender, TextChangedEventArgs e) => ApplyFilter();

    void Grid_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (Grid.SelectedItem is not AuditRow r) return;
        var n = r.Node;
        Detail.Text = $"seq        {r.Seq}\ntime       {n.S("ts")}\nactor      {r.Actor}\naction     {r.Action}\ntarget     {r.Target}\n" +
                      $"outcome    {r.Outcome}\nrun        {n.S("run_id")}\nrequest    {n.S("request_id")}\npolicy     {n.S("policy_version")}\n\n" +
                      $"details\n{Json.Pretty(n["details"])}\n\nprev_hash  {n.S("prev_hash")}\nhash       {n.S("hash")}";
    }

    async void Verify_Click(object sender, RoutedEventArgs e)
    {
        if (shell.Api == null) return;
        await shell.Try(async () =>
        {
            var v = await shell.Api.GetAsync("api/v1/audit/verify");
            var ok = v.B("ok");
            VerifyBanner.Visibility = Visibility.Visible;
            VerifyBanner.Background = (Brush)FindResource(ok ? "SuccessSoft" : "DangerSoft");
            VerifyText.Foreground = (Brush)FindResource(ok ? "Success" : "Danger");
            var extra = (v.L("anchor_seq") > 0 ? $" Retention pruned entries up to #{v.L("anchor_seq")} (anchored)." : "") +
                        (v.L("unchained_rows") > 0 ? $" {v.L("unchained_rows")} entries predate the chain." : "");
            VerifyText.Text = ok
                ? $"✔ Chain intact: {v.L("rows"):N0} entries (#{v.L("first_seq")}–#{v.L("last_seq")}) verified. Head hash {v.S("head_hash")[..16]}….{extra}"
                : $"✖ Chain broken at entry #{v.L("broken_at_seq")}: {v.S("problem")}. The audit log was altered outside the runtime.{extra}";
        });
    }

    async void Reload_Click(object sender, RoutedEventArgs e) => await LoadAsync();

    void ExportJsonl_Click(object sender, RoutedEventArgs e)
    {
        var dlg = new SaveFileDialog { FileName = $"uar-audit-{DateTime.Now:yyyyMMdd-HHmm}.jsonl", Filter = "JSON Lines|*.jsonl" };
        if (dlg.ShowDialog() != true) return;
        // Oldest first, complete records with hashes: an external verifier can recompute the chain.
        File.WriteAllLines(dlg.FileName, all.AsEnumerable().Reverse().Select(r => r.Node.ToJsonString()), Encoding.UTF8);
        shell.Notify($"Exported {all.Count:N0} entries to {dlg.FileName}.", Notice.Success);
    }

    void ExportCsv_Click(object sender, RoutedEventArgs e)
    {
        var dlg = new SaveFileDialog { FileName = $"uar-audit-{DateTime.Now:yyyyMMdd-HHmm}.csv", Filter = "CSV|*.csv" };
        if (dlg.ShowDialog() != true) return;
        var sb = new StringBuilder("seq,time,actor,action,target,outcome,run_id,details,hash\n");
        foreach (var r in all.AsEnumerable().Reverse())
            sb.AppendLine(string.Join(",", new[] { r.Seq.ToString(), r.Node.S("ts"), r.Actor, r.Action, r.Target, r.Outcome, r.RunId,
                r.Node["details"]?.ToJsonString() ?? "", r.Node.S("hash") }.Select(UsagePage.Csv)));
        File.WriteAllText(dlg.FileName, sb.ToString(), Encoding.UTF8);
        shell.Notify($"Exported {all.Count:N0} entries to {dlg.FileName}.", Notice.Success);
    }
}
