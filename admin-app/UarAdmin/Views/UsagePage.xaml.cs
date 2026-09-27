using System.Collections.ObjectModel;
using System.IO;
using System.Text;
using System.Windows;
using System.Windows.Controls;
using Microsoft.Win32;
using UarAdmin.Services;

namespace UarAdmin.Views;

public partial class UsagePage : UserControl, IPage
{
    readonly Shell shell;
    readonly ObservableCollection<UsageRow> rows = new();
    long nextBefore;
    bool loaded;

    public sealed record UsageRow(long Id, string Time, string Subject, string Provider, string Model, long In, long Out,
                                  string Cost, string Estimated, string RunId, string RequestId);

    public UsagePage(Shell shell)
    {
        this.shell = shell;
        InitializeComponent();
        Grid.ItemsSource = rows;
    }

    public async Task ShownAsync()
    {
        if (!loaded) await LoadAsync(reset: true);
    }

    Dictionary<string, string?> Filter()
    {
        var hours = int.Parse((string)((ComboBoxItem)SinceBox.SelectedItem).Tag);
        return new()
        {
            ["subject"] = SubjectBox.Text.Trim(),
            ["model"] = ModelBox.Text.Trim(),
            ["since"] = hours == 0 ? null : DateTimeOffset.UtcNow.AddHours(-hours).ToString("yyyy-MM-ddTHH:mm:ssZ"),
            ["limit"] = "200",
        };
    }

    async Task LoadAsync(bool reset)
    {
        if (shell.Api == null) return;
        await shell.Try(async () =>
        {
            var q = Filter();
            if (!reset && nextBefore > 0) q["before_id"] = nextBefore.ToString();
            var page = await shell.Api.GetAsync("api/v1/usage", q);
            if (reset) rows.Clear();
            foreach (var r in page.Arr("records"))
                rows.Add(new UsageRow(r.L("id"), Json.Local(r.S("ts")), r.S("subject"), r.S("provider"), r.S("model"),
                    r.L("input_tokens"), r.L("output_tokens"),
                    r["cost"] == null ? "unknown" : $"{r["cost"].S("amount")} {r["cost"].S("currency")}",
                    r.B("estimated") ? "yes" : "", r.S("run_id"), r.S("request_id")));
            nextBefore = page.L("next_before_id");
            MoreBtn.IsEnabled = nextBefore > 0;
            TotRequests.Text = page.L("total_requests").ToString("N0");
            TotIn.Text = page.L("total_input_tokens").ToString("N0");
            TotOut.Text = page.L("total_output_tokens").ToString("N0");
            TotCost.Text = $"{page["total_cost"].S("amount", "0")} {page["total_cost"].S("currency")}";
            CountText.Text = $"Showing {rows.Count:N0} of {page.L("total_requests"):N0} calls";
            loaded = true;
        });
    }

    async void Search_Click(object sender, RoutedEventArgs e) => await LoadAsync(reset: true);

    async void More_Click(object sender, RoutedEventArgs e) => await LoadAsync(reset: false);

    void Export_Click(object sender, RoutedEventArgs e)
    {
        var dlg = new SaveFileDialog { FileName = $"uar-inference-{DateTime.Now:yyyyMMdd-HHmm}.csv", Filter = "CSV|*.csv" };
        if (dlg.ShowDialog() != true) return;
        var sb = new StringBuilder("id,time,subject,provider,model,input_tokens,output_tokens,cost,estimated,run_id,request_id\n");
        foreach (var r in rows)
            sb.AppendLine(string.Join(",", new object[] { r.Id, r.Time, r.Subject, r.Provider, r.Model, r.In, r.Out, r.Cost, r.Estimated, r.RunId, r.RequestId }
                .Select(v => Csv(v.ToString() ?? ""))));
        File.WriteAllText(dlg.FileName, sb.ToString(), Encoding.UTF8);
        shell.Notify($"Exported {rows.Count} rows to {dlg.FileName}.", Notice.Success);
    }

    internal static string Csv(string v) => v.IndexOfAny(new[] { ',', '"', '\n' }) >= 0 ? "\"" + v.Replace("\"", "\"\"") + "\"" : v;
}
