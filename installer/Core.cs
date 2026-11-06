using System;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Text;
using System.Web.Script.Serialization;

namespace DevToolkit
{
    /// <summary>An error the user can act on: printed without a stack trace.</summary>
    sealed class UserError : Exception
    {
        public UserError(string message) : base(message) { }
    }

    static class Log
    {
        static readonly object Gate = new object();
        static StreamWriter file;

        public static void OpenFile(string path)
        {
            try
            {
                Directory.CreateDirectory(Path.GetDirectoryName(path));
                file = new StreamWriter(path, true, new UTF8Encoding(false)) { AutoFlush = true };
                file.WriteLine();
                file.WriteLine($"---- {DateTime.Now:o}  dtk {Program.Version}  {Environment.CommandLine}");
            }
            catch { file = null; }
        }

        static void Write(string text, ConsoleColor? color)
        {
            lock (Gate)
            {
                if (color.HasValue) Console.ForegroundColor = color.Value;
                Console.WriteLine(text);
                if (color.HasValue) Console.ResetColor();
                file?.WriteLine(text);
            }
        }

        public static void Head(string s) => Write("== " + s, ConsoleColor.Cyan);
        public static void Info(string s) => Write("   " + s, null);
        public static void Ok(string s) => Write("   ok    " + s, ConsoleColor.Green);
        public static void Warn(string s) => Write("   warn  " + s, ConsoleColor.Yellow);
        public static void Error(string s) => Write("   ERROR " + s, ConsoleColor.Red);
        public static void Plain(string s) => Write(s, null);
        public static void FileOnly(string s) { lock (Gate) file?.WriteLine(s); }
    }

    /// <summary>Command line: a command, --key value pairs, --flags, and everything after "--".</summary>
    sealed class Args
    {
        public string Command = "help";
        readonly Dictionary<string, string> values = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        readonly HashSet<string> flags = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        public List<string> Rest = new List<string>();

        static readonly HashSet<string> ValueOptions = new HashSet<string>(StringComparer.OrdinalIgnoreCase)
        { "from", "root", "groups", "skip", "route", "home" };

        public Args(string[] argv)
        {
            bool haveCommand = false;
            for (int i = 0; i < argv.Length; i++)
            {
                var a = argv[i];
                if (a == "--") { Rest.AddRange(argv.Skip(i + 1)); break; }
                if (a.StartsWith("--") || (a.StartsWith("-") && a.Length == 2) || (a.StartsWith("/") && a.Length == 2))
                {
                    var key = a.TrimStart('-', '/');
                    if (key == "h" || key == "?") key = "help";
                    if (ValueOptions.Contains(key))
                    {
                        if (i + 1 >= argv.Length) throw new UserError($"--{key} needs a value");
                        values[key] = argv[++i];
                    }
                    else flags.Add(key);
                    continue;
                }
                if (!haveCommand) { Command = a.ToLowerInvariant(); haveCommand = true; }
                else Rest.Add(a);
            }
            if (flags.Contains("help")) Command = "help";
        }

        public string Get(string key) => values.TryGetValue(key, out var v) ? v : null;
        public bool Flag(string key) => flags.Contains(key);
        public List<string> List(string key) =>
            (Get(key) ?? "").Split(new[] { ',', ';', ' ' }, StringSplitOptions.RemoveEmptyEntries)
                .Select(s => s.Trim().ToLowerInvariant()).ToList();
    }

    /// <summary>Where the user's profile folders are. A sandbox (--home) redirects all of them for testing.</summary>
    sealed class Profile
    {
        public string UserProfile, AppData, LocalAppData, StartMenuPrograms, FontsDir;
        public bool Sandbox;

        public static Profile Real()
        {
            var p = new Profile
            {
                UserProfile = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),
                AppData = Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData),
                LocalAppData = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                StartMenuPrograms = Environment.GetFolderPath(Environment.SpecialFolder.Programs),
            };
            p.FontsDir = Path.Combine(p.LocalAppData, "Microsoft", "Windows", "Fonts");
            return p;
        }

        public static Profile InSandbox(string home)
        {
            home = Path.GetFullPath(home);
            var up = Path.Combine(home, "profile");
            var p = new Profile
            {
                Sandbox = true,
                UserProfile = up,
                AppData = Path.Combine(up, "AppData", "Roaming"),
                LocalAppData = Path.Combine(up, "AppData", "Local"),
            };
            p.StartMenuPrograms = Path.Combine(p.AppData, "Microsoft", "Windows", "Start Menu", "Programs");
            p.FontsDir = Path.Combine(p.LocalAppData, "Microsoft", "Windows", "Fonts");
            foreach (var d in new[] { p.AppData, p.LocalAppData }) Directory.CreateDirectory(d);
            return p;
        }
    }

    /// <summary>Everything a command needs: options, paths, the release, the user's environment.</summary>
    sealed class Ctx
    {
        public Args A;
        public Profile P;
        public string KitDir;
        public string Root;
        public Kit Kit;
        public UserEnv Env;
        public InstallRecord Rec;
        public string Route;
        /// <summary>Installed folder of each tool, by Scoop app name (e.g. "vscode"), whichever route put it there.</summary>
        public Dictionary<string, string> AppDirs = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        public List<string> Problems = new List<string>();

        public static Ctx Create(Args a, bool needKit, bool needRoot = true)
        {
            var c = new Ctx { A = a };
            c.P = a.Get("home") != null ? Profile.InSandbox(a.Get("home")) : Profile.Real();
            c.KitDir = Path.GetFullPath(a.Get("from") ?? AppDomain.CurrentDomain.BaseDirectory);
            if (needKit) c.Kit = Kit.Load(c.KitDir);
            var root = a.Get("root") ?? Path.Combine(c.P.LocalAppData, "dev-toolkit");
            c.Root = Path.GetFullPath(Environment.ExpandEnvironmentVariables(root)).TrimEnd('\\');
            if (needRoot)
            {
                c.Env = new UserEnv(c);
                c.Rec = InstallRecord.Load(Path.Combine(c.Root, "installed.json"));
            }
            return c;
        }

        /// <summary>Environment for child processes: this process's (which tracks every change dtk makes) plus the sandbox profile.</summary>
        public Dictionary<string, string> ChildEnv()
        {
            var d = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            foreach (DictionaryEntry e in Environment.GetEnvironmentVariables()) d[(string)e.Key] = (string)e.Value;
            if (P.Sandbox)
            {
                d["USERPROFILE"] = P.UserProfile;
                d["HOME"] = P.UserProfile;
                d["APPDATA"] = P.AppData;
                d["LOCALAPPDATA"] = P.LocalAppData;
            }
            return d;
        }

        public string AppDir(string app) => AppDirs.TryGetValue(app, out var d) ? d : null;
    }

    static class Proc
    {
        public sealed class Result
        {
            public int Code;
            public string Out = "", Err = "";
            public bool Ok => Code == 0;
            public string Tail(int lines = 15)
            {
                var all = (Out + "\n" + Err).Split('\n').Select(l => l.TrimEnd()).Where(l => l.Length > 0).ToList();
                return string.Join(Environment.NewLine, all.Skip(Math.Max(0, all.Count - lines)));
            }
        }

        /// <summary>Quotes one argument the way CommandLineToArgvW reads it back.</summary>
        public static string Q(string s)
        {
            if (string.IsNullOrEmpty(s)) return "\"\"";
            if (s.IndexOfAny(new[] { ' ', '\t', '"' }) < 0) return s;
            var sb = new StringBuilder("\"");
            int slashes = 0;
            foreach (var ch in s)
            {
                if (ch == '\\') { slashes++; continue; }
                if (ch == '"') { sb.Append('\\', slashes * 2 + 1); sb.Append('"'); slashes = 0; continue; }
                sb.Append('\\', slashes); slashes = 0; sb.Append(ch);
            }
            sb.Append('\\', slashes * 2);
            return sb.Append('"').ToString();
        }

        public static string Join(params string[] args) => string.Join(" ", args.Select(Q));

        /// <summary>Runs a program, capturing its output (and copying it to the log). Throws only if it can't start.</summary>
        public static Result Run(string exe, string args, Ctx ctx = null, string cwd = null, int timeoutMs = -1,
            bool echo = false, Encoding encoding = null, IDictionary<string, string> extraEnv = null)
        {
            var psi = new ProcessStartInfo(exe, args ?? "")
            {
                UseShellExecute = false,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                RedirectStandardInput = true,
                CreateNoWindow = true,
                WorkingDirectory = cwd ?? Environment.CurrentDirectory,
            };
            if (encoding != null) { psi.StandardOutputEncoding = encoding; psi.StandardErrorEncoding = encoding; }
            if (ctx != null)
            {
                psi.EnvironmentVariables.Clear();
                foreach (var kv in ctx.ChildEnv()) psi.EnvironmentVariables[kv.Key] = kv.Value;
            }
            if (extraEnv != null) foreach (var kv in extraEnv) psi.EnvironmentVariables[kv.Key] = kv.Value;

            Log.FileOnly($"   $ {exe} {args}");
            var o = new StringBuilder();
            var e = new StringBuilder();
            using (var p = new Process { StartInfo = psi })
            {
                p.OutputDataReceived += (s, ev) =>
                {
                    if (ev.Data == null) return;
                    lock (o) o.AppendLine(ev.Data);
                    if (echo) Log.Info("  | " + ev.Data); else Log.FileOnly("     | " + ev.Data);
                };
                p.ErrorDataReceived += (s, ev) =>
                {
                    if (ev.Data == null) return;
                    lock (e) e.AppendLine(ev.Data);
                    if (echo) Log.Info("  | " + ev.Data); else Log.FileOnly("     ! " + ev.Data);
                };
                p.Start();
                p.StandardInput.Close();
                p.BeginOutputReadLine();
                p.BeginErrorReadLine();
                if (!p.WaitForExit(timeoutMs))
                {
                    try { p.Kill(); } catch { }
                    return new Result { Code = -1, Out = o.ToString(), Err = e + "\n(timed out)" };
                }
                p.WaitForExit();
                return new Result { Code = p.ExitCode, Out = o.ToString(), Err = e.ToString() };
            }
        }

        /// <summary>Runs a .cmd or .bat file through cmd.exe with correct quoting.</summary>
        public static Result RunCmd(string script, string args, Ctx ctx = null, string cwd = null, int timeoutMs = -1, bool echo = false)
        {
            var comspec = Environment.GetEnvironmentVariable("ComSpec") ?? "cmd.exe";
            return Run(comspec, $"/d /s /c \"\"{script}\" {args}\"", ctx, cwd, timeoutMs, echo);
        }
    }

    static class Json
    {
        static JavaScriptSerializer S() => new JavaScriptSerializer { MaxJsonLength = int.MaxValue, RecursionLimit = 256 };

        public static Dictionary<string, object> ParseObject(string text) => (Dictionary<string, object>)S().DeserializeObject(text);
        public static Dictionary<string, object> ReadObject(string path) => ParseObject(File.ReadAllText(path));
        public static string Write(object o) => Pretty(S().Serialize(o));

        public static string Str(this Dictionary<string, object> d, string key) =>
            d != null && d.TryGetValue(key, out var v) && v != null ? Convert.ToString(v, System.Globalization.CultureInfo.InvariantCulture) : null;
        public static bool Bool(this Dictionary<string, object> d, string key) =>
            d != null && d.TryGetValue(key, out var v) && v is bool b && b;
        public static Dictionary<string, object> Obj(this Dictionary<string, object> d, string key) =>
            d != null && d.TryGetValue(key, out var v) ? v as Dictionary<string, object> : null;
        public static List<object> Arr(this Dictionary<string, object> d, string key) =>
            d != null && d.TryGetValue(key, out var v) && v is IEnumerable e && !(v is string) && !(v is IDictionary)
                ? e.Cast<object>().ToList() : new List<object>();
        public static List<string> Strs(this Dictionary<string, object> d, string key) =>
            Arr(d, key).Where(x => x != null).Select(x => Convert.ToString(x)).ToList();

        /// <summary>Indents compact JSON so the files dtk writes stay readable.</summary>
        public static string Pretty(string json)
        {
            var sb = new StringBuilder();
            int indent = 0;
            bool inString = false;
            for (int i = 0; i < json.Length; i++)
            {
                char c = json[i];
                if (inString)
                {
                    sb.Append(c);
                    if (c == '\\' && i + 1 < json.Length) { sb.Append(json[++i]); continue; }
                    if (c == '"') inString = false;
                    continue;
                }
                switch (c)
                {
                    case '"': inString = true; sb.Append(c); break;
                    case '{': case '[':
                        sb.Append(c);
                        if (i + 1 < json.Length && (json[i + 1] == '}' || json[i + 1] == ']')) { sb.Append(json[++i]); break; }
                        sb.Append('\n').Append(' ', ++indent * 2); break;
                    case '}': case ']': sb.Append('\n').Append(' ', --indent * 2).Append(c); break;
                    case ',': sb.Append(",\n").Append(' ', indent * 2); break;
                    case ':': sb.Append(": "); break;
                    default: sb.Append(c); break;
                }
            }
            return sb.Append('\n').ToString();
        }
    }

    static class Fs
    {
        /// <summary>Adds the \\?\ prefix so paths past 260 characters work whatever the machine's long-path setting.</summary>
        public static string Long(string path)
        {
            if (path.StartsWith(@"\\?\")) return path;
            if (path.StartsWith(@"\\")) return @"\\?\UNC\" + path.Substring(2);
            return @"\\?\" + path;
        }

        public static void WriteText(string path, string text)
        {
            Directory.CreateDirectory(Path.GetDirectoryName(path));
            File.WriteAllText(path, text, new UTF8Encoding(false));
        }

        public static void DeleteTree(string dir)
        {
            if (!Directory.Exists(Long(dir))) return;
            foreach (var f in Directory.EnumerateFiles(Long(dir), "*", SearchOption.AllDirectories))
            {
                try { File.SetAttributes(f, FileAttributes.Normal); } catch { }
            }
            Directory.Delete(Long(dir), true);
        }

        public static string Size(long bytes)
        {
            if (bytes >= 1L << 30) return $"{bytes / (double)(1L << 30):0.0} GB";
            if (bytes >= 1L << 20) return $"{bytes / (double)(1L << 20):0} MB";
            return $"{bytes / 1024.0:0} KB";
        }

        /// <summary>The /mnt/c/... form of a Windows path, as WSL sees it.</summary>
        public static string ToWsl(string winPath)
        {
            var full = Path.GetFullPath(winPath);
            if (full.Length < 2 || full[1] != ':') throw new UserError($"WSL can only reach paths on a drive letter: {full}");
            return "/mnt/" + char.ToLowerInvariant(full[0]) + full.Substring(2).Replace('\\', '/');
        }
    }
}
