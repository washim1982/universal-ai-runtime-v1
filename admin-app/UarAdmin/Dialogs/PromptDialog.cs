using System.Windows;
using System.Windows.Controls;

namespace UarAdmin.Dialogs;

/// <summary>A confirmation with an optional text field (comment, reason).</summary>
public sealed class PromptDialog : Window
{
    readonly TextBox input;

    PromptDialog(string title, string message, string label, bool danger, string okText, string initial)
    {
        Title = title;
        Width = 520;
        SizeToContent = SizeToContent.Height;
        ResizeMode = ResizeMode.NoResize;
        WindowStartupLocation = WindowStartupLocation.CenterOwner;
        ShowInTaskbar = false;
        Background = System.Windows.Media.Brushes.White;

        var root = new StackPanel { Margin = new Thickness(22) };
        root.Children.Add(new TextBlock { Text = title, Style = (Style)FindResource("H1"), FontSize = 19 });
        root.Children.Add(new TextBlock { Text = message, TextWrapping = TextWrapping.Wrap, Margin = new Thickness(0, 8, 0, 14) });
        root.Children.Add(new TextBlock { Text = label, FontWeight = FontWeights.SemiBold });
        input = new TextBox { Text = initial, Margin = new Thickness(0, 4, 0, 0), Height = 64, AcceptsReturn = true, TextWrapping = TextWrapping.Wrap,
                              VerticalContentAlignment = VerticalAlignment.Top };
        root.Children.Add(input);
        var buttons = new StackPanel { Orientation = Orientation.Horizontal, HorizontalAlignment = HorizontalAlignment.Right, Margin = new Thickness(0, 16, 0, 0) };
        buttons.Children.Add(new Button { Content = "Cancel", IsCancel = true });
        var ok = new Button { Content = okText, IsDefault = true, Margin = new Thickness(0), Style = (Style)FindResource(danger ? "DangerButton" : "Primary") };
        ok.Click += (_, _) => DialogResult = true;
        buttons.Children.Add(ok);
        root.Children.Add(buttons);
        Content = root;
        Loaded += (_, _) => input.Focus();
    }

    /// <summary>Returns the entered text (possibly empty), or null when cancelled.</summary>
    public static string? Ask(Window? owner, string title, string message, string label, bool danger = false,
                              string okText = "OK", string initial = "")
    {
        var d = new PromptDialog(title, message, label, danger, okText, initial) { Owner = owner };
        return d.ShowDialog() == true ? d.input.Text.Trim() : null;
    }
}
