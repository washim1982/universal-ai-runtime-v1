using System.Windows;
using System.Windows.Threading;

namespace UarAdmin;

public partial class App : Application
{
    protected override void OnStartup(StartupEventArgs e)
    {
        DispatcherUnhandledException += OnUnhandled;
        base.OnStartup(e);
    }

    static void OnUnhandled(object sender, DispatcherUnhandledExceptionEventArgs e)
    {
        MessageBox.Show(e.Exception.Message, "UAR Admin", MessageBoxButton.OK, MessageBoxImage.Error);
        e.Handled = true;
    }
}
