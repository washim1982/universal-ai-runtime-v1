using System.Text.Json.Nodes;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Threading;
using UarAdmin.Dialogs;
using UarAdmin.Services;

namespace UarAdmin.Views;

public partial class ApprovalsPage : UserControl, IPage
{
    readonly Shell shell;
    readonly DispatcherTimer timer = new();
    string? selectedId;

    public sealed record ApprovalRow(string Id, string Created, string Action, string Kind, string RequestedBy, string Approvers,
                                     string Status, string Expires, JsonNode Node);

    public ApprovalsPage(Shell shell)
    {
        this.shell = shell;
        InitializeComponent();
        timer.Tick += async (_, _) => { if (AutoBox.IsChecked == true) await LoadAsync(quiet: true); };
    }

    public async Task ShownAsync()
    {
        timer.Interval = TimeSpan.FromSeconds(Math.Max(3, shell.Settings.RefreshSeconds));
        timer.Start();
        await LoadAsync();
    }

    public void Hidden() => timer.Stop();

    async Task LoadAsync(bool quiet = false)
    {
        if (shell.Api == null) return;
        var status = (string)((ComboBoxItem)StatusBox.SelectedItem).Content;
        async Task Load()
        {
            var list = await shell.Api.GetAsync("api/v1/approvals", new Dictionary<string, string?> { ["status"] = status == "all" ? null : status });
            var rows = list.Arr("approvals").Select(a => new ApprovalRow(
                a.S("approval_id"), Json.Local(a.S("created_at")), a.S("action"), a.S("kind"), a.S("requested_by"),
                string.Join(", ", a.Arr("approver_roles").Select(x => x.ToString())) is { Length: > 0 } r ? r : "any approver",
                a.S("status"), Json.Local(a.S("expires_at")), a)).ToList();
            Grid.ItemsSource = rows;
            Grid.SelectedItem = rows.FirstOrDefault(x => x.Id == selectedId) ?? rows.FirstOrDefault();
            if (Grid.SelectedItem == null) ShowDetail(null);
        }
        if (quiet) { try { await Load(); } catch (ApiException) { } }
        else await shell.Try(Load);
    }

    void Grid_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (Grid.SelectedItem is ApprovalRow row) { selectedId = row.Id; ShowDetail(row); }
    }

    void ShowDetail(ApprovalRow? row)
    {
        if (row == null)
        {
            DTitle.Text = "Select an approval";
            DMeta.Text = DDecision.Text = DSummary.Text = "";
            ApproveBtn.IsEnabled = RejectBtn.IsEnabled = false;
            return;
        }
        var a = row.Node;
        DTitle.Text = $"{row.Action}  ({(row.Kind == "tool" ? "tool call" : "approval step")})";
        DMeta.Text = $"{row.Id} · run {a.S("run_id")} · node {a.S("node_id")}\nrequested by {row.RequestedBy} · approvers: {row.Approvers}\nexpires {row.Expires} · args hash {Short(a.S("args_hash"))}…";
        DSummary.Text = Json.Pretty(a["summary"]);
        var decided = a.S("decided_by") != "";
        DDecision.Text = decided
            ? $"{a.S("status")} by {a.S("decided_by")} at {Json.Local(a.S("decided_at"))}{(a.S("comment") != "" ? $": \"{a.S("comment")}\"" : "")}{(a.B("consumed") ? " · action performed" : "")}"
            : row.Status == "pending" ? "Review the action and its arguments above before deciding." : $"Status: {row.Status}";
        ApproveBtn.IsEnabled = RejectBtn.IsEnabled = row.Status == "pending";
    }

    static string Short(string hash) => hash.Length > 16 ? hash[..16] : hash;

    async Task DecideAsync(bool approve)
    {
        if (Grid.SelectedItem is not ApprovalRow row || shell.Api == null) return;
        var comment = PromptDialog.Ask(Window.GetWindow(this), approve ? "Approve" : "Reject",
            $"{(approve ? "Approve" : "Reject")} {row.Action} for run {row.Node.S("run_id")}?\nThe decision is bound to the arguments shown (hash {Short(row.Node.S("args_hash"))}…) and cannot be changed afterwards.",
            "Comment (recorded in the audit trail)", danger: !approve, okText: approve ? "Approve" : "Reject");
        if (comment == null) return;
        await shell.Try(async () =>
        {
            await shell.Api.PostAsync($"api/v1/approvals/{Uri.EscapeDataString(row.Id)}/decision",
                new { approve, comment, args_hash = row.Node.S("args_hash") });
            await LoadAsync();
        }, approve ? $"Approved {row.Action}; the run continues." : $"Rejected {row.Action}.");
    }

    async void Approve_Click(object sender, RoutedEventArgs e) => await DecideAsync(true);
    async void Reject_Click(object sender, RoutedEventArgs e) => await DecideAsync(false);
    async void Refresh_Click(object sender, RoutedEventArgs e) => await LoadAsync();
    async void Status_Changed(object sender, SelectionChangedEventArgs e) { if (IsLoaded) await LoadAsync(); }
}
