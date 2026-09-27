using System.Text.RegularExpressions;
using System.Windows;
using System.Windows.Controls;

namespace UarAdmin.Dialogs;

public partial class CreateKeyDialog : Window
{
    public sealed class RoleChoice
    {
        public string Name { get; init; } = "";
        public bool Selected { get; set; }
    }

    readonly List<RoleChoice> roles;
    public string Subject => SubjectBox.Text.Trim();
    public string[] Roles => roles.Where(r => r.Selected).Select(r => r.Name).ToArray();
    public string Description => DescriptionBox.Text.Trim();
    public int ExpiresInDays => int.Parse((string)((ComboBoxItem)ExpiryBox.SelectedItem).Tag);

    public CreateKeyDialog(IEnumerable<string> roleNames)
    {
        InitializeComponent();
        roles = roleNames.Select(n => new RoleChoice { Name = n }).ToList();
        RoleList.ItemsSource = roles;
        Loaded += (_, _) => SubjectBox.Focus();
    }

    void Create_Click(object sender, RoutedEventArgs e)
    {
        if (!Regex.IsMatch(Subject, @"^[A-Za-z0-9._@:+-]{1,128}$"))
            ErrorText.Text = "Enter a subject: letters, digits and . _ @ : + - (no spaces).";
        else if (Roles.Length == 0)
            ErrorText.Text = "Choose at least one role.";
        else if (Roles.Contains("admin") && MessageBox.Show(this, "The admin role can manage keys, approvals and plugins. Create an admin key?",
                                                            "New API key", MessageBoxButton.YesNo, MessageBoxImage.Warning) != MessageBoxResult.Yes)
            return;
        else
            DialogResult = true;
    }
}
