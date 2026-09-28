using System.Windows;

namespace UarAdmin.Services;

public enum Notice { Info, Success, Warning, Error }

/// <summary>What every page shares: settings, the API client for the active connection, the runtime
/// manager, and a way to show a notice.</summary>
public sealed class Shell
{
    public AppSettings Settings { get; }
    public RuntimeManager Runtime { get; }
    public ApiClient? Api { get; private set; }
    public ConnectionProfile? Profile => Settings.Active;

    public event Action? ConnectionChanged;
    public event Action<string, Notice>? Notified;

    public Shell()
    {
        Settings = SettingsStore.Load();
        Runtime = new RuntimeManager(() => Settings);
        Reconnect();
    }

    public void Reconnect()
    {
        Api?.Dispose();
        var p = Settings.Active;
        Api = p == null ? null : ApiClient.For(p);
        ConnectionChanged?.Invoke();
    }

    public void Save() => SettingsStore.Save(Settings);

    public void Notify(string message, Notice kind = Notice.Info) => Notified?.Invoke(message, kind);

    /// <summary>Run an action, turning failures into a notice instead of a crash.</summary>
    public async Task<bool> Try(Func<Task> action, string? success = null)
    {
        try
        {
            await action();
            if (success != null) Notify(success, Notice.Success);
            return true;
        }
        catch (ApiException e)
        {
            Notify(e.Status == 0 ? e.Message : $"{e.Message} ({e.Code})", e.Status is 401 or 403 ? Notice.Warning : Notice.Error);
        }
        catch (Exception e) when (e is InvalidOperationException or TimeoutException or System.IO.IOException)
        {
            Notify(e.Message, Notice.Error);
        }
        return false;
    }

    public static bool Confirm(string text, string title = "UAR Admin") =>
        MessageBox.Show(text, title, MessageBoxButton.YesNo, MessageBoxImage.Question) == MessageBoxResult.Yes;
}

/// <summary>A page in the main window.</summary>
public interface IPage
{
    Task ShownAsync();
    void Hidden() { }
}
