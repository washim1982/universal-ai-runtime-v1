using System.Diagnostics;
using System.IO;
using System.Text;
using System.Text.RegularExpressions;

namespace UarAdmin.Services;

public enum RuntimeMode { Stopped, Starting, Managed, External, Docker }

/// <summary>Starts, stops and restarts the runtime on this machine.
///  - Managed: started by this app (python -m uar_runtime.main serve), output captured to a log file.
///  - External: something else started it (setup dashboard, a terminal); stopped by process id.
///  - Docker: the compose "runtime" container; controlled with docker stop/start.</summary>
public sealed class RuntimeManager
{
    readonly Func<AppSettings> settings;
    Process? proc;
    string? stoppedDocker;
    readonly object gate = new();
    readonly LinkedList<string> output = new();
    const int MaxLines = 5000;

    public event Action<string>? OutputLine;
    public RuntimeMode Mode { get; private set; } = RuntimeMode.Stopped;
    public int? Pid { get; private set; }
    public string Detail { get; private set; } = "";
    public string? DockerContainer { get; private set; }
    public string LogFile { get; private set; } = "";

    public RuntimeManager(Func<AppSettings> settings) { this.settings = settings; }

    public bool OwnsProcess => proc is { HasExited: false };

    public string[] OutputSnapshot()
    {
        lock (gate) return output.ToArray();
    }

    string Root => settings().RuntimeRoot;
    /// <summary>The connection points at this machine (only then can the app start or stop the runtime).</summary>
    public bool IsLocal => settings().Active is not { } p || (Uri.TryCreate(p.BaseUrl, UriKind.Absolute, out var u) && u.IsLoopback);

    int Port => settings().Active is { } p && Uri.TryCreate(p.BaseUrl, UriKind.Absolute, out var u) && u.IsLoopback
        ? u.Port : SettingsStore.LocalHttpPort(settings());

    public string PythonPath => Path.Combine(Root, ".venv", "Scripts", "python.exe");

    public string CommandLine => $"\"{PythonPath}\" -m uar_runtime.main serve --config {settings().ConfigPath}";

    /// <summary>Work out how the runtime at the active profile's URL is running (if it is).</summary>
    public async Task RefreshAsync(ApiClient? api)
    {
        if (OwnsProcess) { Mode = RuntimeMode.Managed; Pid = proc!.Id; Detail = "started by UAR Admin"; return; }
        var healthy = api != null && await api.HealthyAsync();
        if (!IsLocal)
        {
            Mode = healthy ? RuntimeMode.External : RuntimeMode.Stopped;
            Pid = null; DockerContainer = null;
            Detail = healthy ? "remote runtime - start and stop it on its own host" : "";
            return;
        }
        DockerContainer = await FindDockerRuntimeAsync();
        if (!healthy)
        {
            if (Mode != RuntimeMode.Starting) { Mode = RuntimeMode.Stopped; Pid = null; Detail = ""; }
            return;
        }
        var pid = FindListeningPid(Port);
        if (DockerContainer != null && (pid == null || IsDockerProxy(pid.Value)))
        {
            Mode = RuntimeMode.Docker; Pid = null; Detail = $"container {DockerContainer}";
            return;
        }
        Mode = RuntimeMode.External; Pid = pid;
        Detail = pid != null ? $"{ProcessName(pid.Value)} (pid {pid}) started outside UAR Admin" : "running elsewhere";
    }

    public async Task StartAsync(ApiClient? api, CancellationToken ct = default)
    {
        await RefreshAsync(api);
        if (Mode is RuntimeMode.Managed or RuntimeMode.External or RuntimeMode.Docker)
            throw new InvalidOperationException("The runtime is already running.");
        if (!IsLocal) throw new InvalidOperationException("This connection points at another machine; UAR Admin starts local runtimes only.");
        if (stoppedDocker != null)   // this app stopped the Docker runtime: bring the same container back
        {
            var name = stoppedDocker;
            await RunAsync("docker", $"start {name}");
            stoppedDocker = null;
            await WaitHealthyAsync(api, ct);
            return;
        }
        if (!SettingsStore.IsRuntimeRoot(Root)) throw new InvalidOperationException("Set the runtime folder in Settings first.");
        if (!File.Exists(PythonPath)) throw new InvalidOperationException($"Python environment not found: {PythonPath}. Run the setup dashboard first.");
        var cfg = Path.Combine(Root, settings().ConfigPath);
        if (!File.Exists(cfg)) throw new InvalidOperationException($"Configuration not found: {cfg}. Run scripts/bootstrap_local.py (setup dashboard step) first.");
        if (FindListeningPid(Port) is { } busy) throw new InvalidOperationException($"Port {Port} is already used by {ProcessName(busy)} (pid {busy}).");

        Directory.CreateDirectory(SettingsStore.LogFolder);
        LogFile = Path.Combine(SettingsStore.LogFolder, $"runtime-{DateTime.Now:yyyyMMdd}.log");
        var psi = new ProcessStartInfo(PythonPath, $"-m uar_runtime.main serve --config \"{settings().ConfigPath}\"")
        {
            WorkingDirectory = Root,
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            StandardOutputEncoding = Encoding.UTF8,
            StandardErrorEncoding = Encoding.UTF8,
        };
        foreach (var (k, v) in SettingsStore.ReadCredentials(Root))
            if (k.StartsWith("UAR_") && !k.EndsWith("_KEY")) psi.Environment[k] = v;   // e.g. UAR_JWT_SECRET; never API keys
        psi.Environment["PYTHONUNBUFFERED"] = "1";
        psi.Environment["PYTHONIOENCODING"] = "utf-8";
        var p = new Process { StartInfo = psi, EnableRaisingEvents = true };
        p.OutputDataReceived += (_, e) => { if (e.Data != null) Append(e.Data); };
        p.ErrorDataReceived += (_, e) => { if (e.Data != null) Append(e.Data); };
        p.Exited += (_, _) => Append($"== runtime process exited (code {SafeExitCode(p)}) at {DateTime.Now:HH:mm:ss}");
        Append($"== starting: {CommandLine}  (in {Root}) at {DateTime.Now:yyyy-MM-dd HH:mm:ss}");
        p.Start();
        p.BeginOutputReadLine();
        p.BeginErrorReadLine();
        proc = p;
        Mode = RuntimeMode.Starting; Pid = p.Id; Detail = "starting";
        await WaitHealthyAsync(api, ct);
        Mode = RuntimeMode.Managed; Detail = "started by UAR Admin";
    }

    async Task WaitHealthyAsync(ApiClient? api, CancellationToken ct)
    {
        for (var i = 0; i < 120; i++)
        {
            if (api != null && await api.HealthyAsync(ct)) return;
            if (proc is { HasExited: true }) throw new InvalidOperationException("The runtime exited during startup. See Service logs → Process output.");
            await Task.Delay(500, ct);
        }
        throw new TimeoutException("The runtime did not become healthy within 60 seconds.");
    }

    public async Task StopAsync(ApiClient? api)
    {
        await RefreshAsync(api);
        switch (Mode)
        {
            case RuntimeMode.Managed:
            case RuntimeMode.Starting when proc != null:
                Append($"== stopping (pid {proc!.Id}) at {DateTime.Now:HH:mm:ss}");
                KillTree(proc.Id);
                await Task.Run(() => proc.WaitForExit(10000));
                proc = null;
                break;
            case RuntimeMode.External when Pid != null:
                KillTree(Pid.Value);
                break;
            case RuntimeMode.Docker when DockerContainer != null:
                await RunAsync("docker", $"stop {DockerContainer}");
                stoppedDocker = DockerContainer;
                break;
            case RuntimeMode.External:
                throw new InvalidOperationException(IsLocal ? "The runtime answers but its process could not be identified; stop it where it was started."
                                                            : "This connection points at another machine; stop the runtime on its host.");
        }
        for (var i = 0; i < 40 && api != null && await api.HealthyAsync(); i++) await Task.Delay(250);
        Mode = RuntimeMode.Stopped; Pid = null; Detail = "";
    }

    public async Task RestartAsync(ApiClient? api, CancellationToken ct = default)
    {
        await RefreshAsync(api);
        if (Mode != RuntimeMode.Stopped) await StopAsync(api);
        await StartAsync(api, ct);
    }

    void Append(string line)
    {
        lock (gate)
        {
            output.AddLast(line);
            while (output.Count > MaxLines) output.RemoveFirst();
            try { if (LogFile != "") File.AppendAllText(LogFile, line + Environment.NewLine); } catch (IOException) { }
        }
        OutputLine?.Invoke(line);
    }

    static int SafeExitCode(Process p) { try { return p.ExitCode; } catch { return -1; } }

    static void KillTree(int pid)
    {
        try { Process.GetProcessById(pid).Kill(entireProcessTree: true); }
        catch (ArgumentException) { }   // already gone
    }

    public static string ProcessName(int pid)
    {
        try { return Process.GetProcessById(pid).ProcessName; } catch { return "process"; }
    }

    static bool IsDockerProxy(int pid) => ProcessName(pid).Contains("docker", StringComparison.OrdinalIgnoreCase)
                                           || ProcessName(pid).Contains("wslrelay", StringComparison.OrdinalIgnoreCase)
                                           || ProcessName(pid).Contains("com.docker", StringComparison.OrdinalIgnoreCase);

    /// <summary>The process listening on a local TCP port (netstat -ano).</summary>
    public static int? FindListeningPid(int port)
    {
        try
        {
            var psi = new ProcessStartInfo("netstat", "-ano -p TCP") { RedirectStandardOutput = true, UseShellExecute = false, CreateNoWindow = true };
            using var p = Process.Start(psi)!;
            var text = p.StandardOutput.ReadToEnd();
            p.WaitForExit(5000);
            var rx = new Regex($@"^\s*TCP\s+\S+:{port}\s+\S+\s+LISTENING\s+(\d+)", RegexOptions.Multiline);
            var m = rx.Match(text);
            return m.Success ? int.Parse(m.Groups[1].Value) : null;
        }
        catch (Exception) { return null; }
    }

    /// <summary>The running container that publishes the runtime's HTTP port (compose service "uar").</summary>
    async Task<string?> FindDockerRuntimeAsync()
    {
        var (rc, text) = await RunAsync("docker", $"ps --filter publish={Port} --format {{{{.Names}}}}", quiet: true);
        return rc == 0 ? text.Split('\n', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries).FirstOrDefault() : null;
    }

    public static async Task<(int, string)> RunAsync(string file, string args, bool quiet = false)
    {
        try
        {
            var psi = new ProcessStartInfo(file, args) { RedirectStandardOutput = true, RedirectStandardError = true, UseShellExecute = false, CreateNoWindow = true };
            using var p = Process.Start(psi)!;
            var outTask = p.StandardOutput.ReadToEndAsync();
            var errTask = p.StandardError.ReadToEndAsync();
            using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(60));
            await p.WaitForExitAsync(cts.Token);
            var text = await outTask + await errTask;
            if (p.ExitCode != 0 && !quiet) throw new InvalidOperationException($"{file} {args} failed: {text.Trim()}");
            return (p.ExitCode, text);
        }
        catch (Exception) when (quiet) { return (-1, ""); }
    }

    /// <summary>Last lines of a Docker container's log (for the Docker mode of Service logs).</summary>
    public async Task<string[]> DockerLogsAsync(int tail = 500)
    {
        if (DockerContainer == null) return Array.Empty<string>();
        var (_, text) = await RunAsync("docker", $"logs --tail {tail} {DockerContainer}", quiet: true);
        return text.Split('\n');
    }
}
