using System.Text.RegularExpressions;
using System.Windows;
using System.Windows.Controls;

namespace UarAdmin.Dialogs;

public partial class RegisterAppDialog : Window
{
    readonly List<CreateKeyDialog.RoleChoice> roles;
    public string AppName => NameBox.Text.Trim();
    public string Description => DescriptionBox.Text.Trim();
    public string[] Roles => roles.Where(r => r.Selected).Select(r => r.Name).ToArray();
    public int TokenTtlSeconds => int.Parse((string)((ComboBoxItem)TtlBox.SelectedItem).Tag);
    public int SecretDays => int.Parse((string)((ComboBoxItem)SecretBox.SelectedItem).Tag);

    public RegisterAppDialog(IEnumerable<string> roleNames)
    {
        InitializeComponent();
        roles = roleNames.Select(n => new CreateKeyDialog.RoleChoice { Name = n }).ToList();
        RoleList.ItemsSource = roles;
        Loaded += (_, _) => NameBox.Focus();
    }

    void Register_Click(object sender, RoutedEventArgs e)
    {
        if (!Regex.IsMatch(AppName, @"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}$"))
            ErrorText.Text = "Enter a name: letters, digits, spaces and . _ - (up to 64 characters).";
        else if (Roles.Length == 0)
            ErrorText.Text = "Choose at least one role.";
        else if (Roles.Contains("admin") && MessageBox.Show(this, "Tokens of this application will be able to manage keys, applications and approvals. Continue?",
                                                            "Register application", MessageBoxButton.YesNo, MessageBoxImage.Warning) != MessageBoxResult.Yes)
            return;
        else
            DialogResult = true;
    }
}
