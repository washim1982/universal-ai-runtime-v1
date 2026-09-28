using System.IO;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;

namespace UarAdmin.Services;

/// <summary>A runtime connection: base URL plus an API key (stored encrypted with Windows DPAPI).</summary>
public sealed class ConnectionProfile
{
    public string Name { get; set; } = "";
    public string BaseUrl { get; set; } = "http://127.0.0.1:9000";
    public string ProtectedKey { get; set; } = "";
    /// <summary>"apikey" (X-API-Key) or "app" (registered application: client credentials -> access tokens).</summary>
    public string AuthType { get; set; } = "apikey";
    public string ClientId { get; set; } = "";
    public string ProtectedSecret { get; set; } = "";

    public string ApiKey
    {
        get => Unprotect(ProtectedKey);
        set => ProtectedKey = Protect(value);
    }

    public string ClientSecret
    {
        get => Unprotect(ProtectedSecret);
        set => ProtectedSecret = Protect(value);
    }

    public bool IsApp => AuthType == "app";

    public override string ToString() => Name;

    static readonly byte[] Entropy = Encoding.UTF8.GetBytes("uar-admin-app/v1");

    static string Protect(string plain) =>
        string.IsNullOrEmpty(plain) ? "" :
        Convert.ToBase64String(ProtectedData.Protect(Encoding.UTF8.GetBytes(plain), Entropy, DataProtectionScope.CurrentUser));

    static string Unprotect(string blob)
    {
        if (string.IsNullOrEmpty(blob)) return "";
        try { return Encoding.UTF8.GetString(ProtectedData.Unprotect(Convert.FromBase64String(blob), Entropy, DataProtectionScope.CurrentUser)); }
        catch (CryptographicException) { return ""; }   // settings copied from another user or machine
    }
}

public sealed class AppSettings
{
    public List<ConnectionProfile> Profiles { get; set; } = new();
    public string ActiveProfile { get; set; } = "";
    public string RuntimeRoot { get; set; } = "";
    public string ConfigPath { get; set; } = @".local\uar.yaml";
    public int RefreshSeconds { get; set; } = 5;

    public ConnectionProfile? Active => Profiles.FirstOrDefault(p => p.Name == ActiveProfile) ?? Profiles.FirstOrDefault();
}

/// <summary>Settings live in %APPDATA%\UAR\Admin\settings.json (per Windows user). API keys are
/// DPAPI-encrypted, so the file is useless on another account or machine.</summary>
public static class SettingsStore
{
    // UAR_ADMIN_SETTINGS_DIR overrides the location (portable use, tests).
    public static string Folder { get; } = Environment.GetEnvironmentVariable("UAR_ADMIN_SETTINGS_DIR") is { Length: > 0 } dir ? dir
        : Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "UAR", "Admin");
    public static string LogFolder { get; } = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "UAR", "Admin", "logs");
    static string FilePath => Path.Combine(Folder, "settings.json");
    static readonly JsonSerializerOptions Json = new() { WriteIndented = true };

    public static AppSettings Load()
    {
        AppSettings s;
        try { s = File.Exists(FilePath) ? JsonSerializer.Deserialize<AppSettings>(File.ReadAllText(FilePath)) ?? new() : new(); }
        catch (Exception) { s = new(); }
        if (string.IsNullOrEmpty(s.RuntimeRoot)) s.RuntimeRoot = DetectRuntimeRoot() ?? "";
        if (s.Profiles.Count == 0) ImportLocalProfile(s);
        return s;
    }

    public static void Save(AppSettings s)
    {
        Directory.CreateDirectory(Folder);
        File.WriteAllText(FilePath, JsonSerializer.Serialize(s, Json));
    }

    /// <summary>The repository folder: walk up from the executable, then try the current directory.</summary>
    public static string? DetectRuntimeRoot()
    {
        foreach (var start in new[] { AppContext.BaseDirectory, Environment.CurrentDirectory })
        {
            var d = new DirectoryInfo(start);
            while (d != null)
            {
                if (IsRuntimeRoot(d.FullName)) return d.FullName;
                d = d.Parent;
            }
        }
        return null;
    }

    public static bool IsRuntimeRoot(string path) =>
        Directory.Exists(Path.Combine(path, "runtime", "uar_runtime")) && Directory.Exists(Path.Combine(path, "proto"));

    /// <summary>First run on a machine set up with scripts/bootstrap_local.py: use its admin key.</summary>
    public static bool ImportLocalProfile(AppSettings s)
    {
        if (string.IsNullOrEmpty(s.RuntimeRoot)) return false;
        var creds = ReadCredentials(s.RuntimeRoot);
        if (!creds.TryGetValue("UAR_ADMIN_KEY", out var key) || string.IsNullOrWhiteSpace(key)) return false;
        var p = new ConnectionProfile { Name = "Local runtime (admin@acme)", BaseUrl = $"http://127.0.0.1:{LocalHttpPort(s)}" };
        p.ApiKey = key.Trim();
        s.Profiles.RemoveAll(x => x.Name == p.Name);
        s.Profiles.Insert(0, p);
        s.ActiveProfile = p.Name;
        return true;
    }

    public static Dictionary<string, string> ReadCredentials(string root)
    {
        var path = Path.Combine(root, ".local", "credentials.env");
        var d = new Dictionary<string, string>();
        if (!File.Exists(path)) return d;
        foreach (var line in File.ReadAllLines(path))
        {
            var i = line.IndexOf('=');
            if (i > 0 && !line.TrimStart().StartsWith('#')) d[line[..i].Trim()] = line[(i + 1)..].Trim();
        }
        return d;
    }

    public static int LocalHttpPort(AppSettings s)
    {
        try
        {
            var cfg = Path.Combine(s.RuntimeRoot, s.ConfigPath);
            var m = Regex.Match(File.ReadAllText(cfg), @"http_port:\s*(\d+)");
            if (m.Success) return int.Parse(m.Groups[1].Value);
        }
        catch (Exception) { }
        return 9000;
    }
}
