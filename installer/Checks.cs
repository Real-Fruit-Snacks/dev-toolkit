using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.IO;
using System.Linq;
using System.Text;
using Microsoft.Win32;

namespace DevToolkit
{
    /// <summary>What this machine allows. Every install starts here; `dtk check` prints it.</summary>
    sealed class Checks
    {
        public string Windows;
        public bool Is64;
        public bool ExeOk;
        public string ExeDetail;
        public string PowerShell;
        public string LanguageMode;
        public Dictionary<string, string> Policies = new Dictionary<string, string>();
        public bool ScriptsOk;
        public string ScriptsDetail;
        public bool Msvc;
        public string MsvcDetail;
        public bool Wsl;
        public string WslDetail;
        public List<string> WslDistros = new List<string>();
        public bool LongPaths;
        public long FreeBytes;
        public List<string> OnPath = new List<string>();

        public bool ScoopPossible => ScriptsOk && LanguageMode == "FullLanguage";

        public static Checks Run(Ctx ctx)
        {
            var c = new Checks();
            c.Windows = WindowsVersion();
            c.Is64 = Environment.Is64BitOperatingSystem;
            c.ProbeExe(ctx);
            c.ProbePowerShell(ctx);
            c.ProbeMsvc();
            c.ProbeWsl();
            c.LongPaths = RegDword(Registry.LocalMachine, @"SYSTEM\CurrentControlSet\Control\FileSystem", "LongPathsEnabled") == 1;
            try
            {
                var drive = Path.GetPathRoot(ctx.Root);
                c.FreeBytes = new DriveInfo(drive).AvailableFreeSpace;
            }
            catch { c.FreeBytes = -1; }
            foreach (var tool in new[] { "git", "python", "node", "dotnet", "cargo", "code", "scoop" })
            {
                var hit = Which(tool);
                if (hit != null) c.OnPath.Add($"{tool} -> {hit}");
            }
            return c;
        }

        static string WindowsVersion()
        {
            using (var k = Registry.LocalMachine.OpenSubKey(@"SOFTWARE\Microsoft\Windows NT\CurrentVersion"))
            {
                var name = k?.GetValue("ProductName") as string ?? "Windows";
                var build = k?.GetValue("CurrentBuildNumber") as string ?? "?";
                var ubr = k?.GetValue("UBR");
                if (int.TryParse(build, out var b) && b >= 22000) name = name.Replace("Windows 10", "Windows 11");
                return $"{name} build {build}{(ubr != null ? "." + ubr : "")}";
            }
        }

        static int? RegDword(RegistryKey hive, string path, string name)
        {
            using (var k = hive.OpenSubKey(path)) return k?.GetValue(name) as int?;
        }

        static string RegString(RegistryKey hive, string path, string name)
        {
            using (var k = hive.OpenSubKey(path)) return k?.GetValue(name) as string;
        }

        public static string Which(string tool)
        {
            foreach (var dir in UserEnv.SplitPath(Environment.GetEnvironmentVariable("PATH")))
                foreach (var ext in new[] { ".exe", ".cmd", ".bat" })
                {
                    try
                    {
                        var p = Path.Combine(Environment.ExpandEnvironmentVariables(dir), tool + ext);
                        if (File.Exists(p)) return p;
                    }
                    catch { }
                }
            return null;
        }

        /// <summary>Copies dtk.exe into the install folder and runs the copy: the same thing every tool in the kit needs.</summary>
        void ProbeExe(Ctx ctx)
        {
            var dir = Path.Combine(ctx.Root, ".probe");
            bool madeRoot = !Directory.Exists(ctx.Root);
            try
            {
                Directory.CreateDirectory(dir);
                var copy = Path.Combine(dir, "probe.exe");
                File.Copy(typeof(Checks).Assembly.Location, copy, true);
                var r = Proc.Run(copy, "ping", timeoutMs: 60000);
                ExeOk = r.Ok && r.Out.Contains("pong");
                ExeDetail = ExeOk ? $"a program copied to {dir} runs" : $"the copy started but didn't answer (exit {r.Code}) {r.Tail(3)}";
            }
            catch (Win32Exception e)
            {
                ExeOk = false;
                var why = e.NativeErrorCode == 1260 ? "blocked by group policy (AppLocker or Software Restriction Policies)"
                    : e.NativeErrorCode == 4551 ? "blocked by Windows Defender Application Control"
                    : e.NativeErrorCode == 225 ? "blocked by antivirus"
                    : e.Message;
                ExeDetail = $"programs in {dir} can't run: {why} (error {e.NativeErrorCode})";
            }
            catch (Exception e)
            {
                ExeOk = false;
                ExeDetail = $"couldn't test running programs from {dir}: {e.Message}";
            }
            finally
            {
                try { Directory.Delete(dir, true); } catch { }
                if (madeRoot) try { Directory.Delete(ctx.Root, false); } catch { }
            }
        }

        void ProbePowerShell(Ctx ctx)
        {
            var sys = Environment.GetFolderPath(Environment.SpecialFolder.System);
            var ps = Path.Combine(sys, @"WindowsPowerShell\v1.0\powershell.exe");
            if (!File.Exists(ps)) { PowerShell = null; ScriptsDetail = "Windows PowerShell isn't on this machine"; return; }
            PowerShell = ps;

            const string pol = @"SOFTWARE\Policies\Microsoft\Windows\PowerShell";
            const string shell = @"SOFTWARE\Microsoft\PowerShell\1\ShellIds\Microsoft.PowerShell";
            Policies["MachinePolicy"] = PolicyValue(Registry.LocalMachine, pol);
            Policies["UserPolicy"] = PolicyValue(Registry.CurrentUser, pol);
            Policies["CurrentUser"] = RegString(Registry.CurrentUser, shell, "ExecutionPolicy") ?? "Undefined";
            Policies["LocalMachine"] = RegString(Registry.LocalMachine, shell, "ExecutionPolicy") ?? "Undefined";

            // Typed commands are never subject to the script policy, so this reports the language mode either way.
            try
            {
                var r = Proc.Run(ps, "-NoLogo -NoProfile -NonInteractive -Command \"$ExecutionContext.SessionState.LanguageMode.ToString()\"", timeoutMs: 120000);
                LanguageMode = r.Out.Trim().Split('\n').LastOrDefault()?.Trim();
                if (string.IsNullOrEmpty(LanguageMode)) LanguageMode = "unknown";
            }
            catch (Exception e) { LanguageMode = "unknown (" + e.Message + ")"; }

            // The real test: can a local script run? Scoop is made of scripts.
            var dir = Path.Combine(Path.GetTempPath(), "dev-toolkit-probe-" + Guid.NewGuid().ToString("N").Substring(0, 8));
            try
            {
                Directory.CreateDirectory(dir);
                var script = Path.Combine(dir, "probe.ps1");
                File.WriteAllText(script, "Write-Output ('script-ok ' + $ExecutionContext.SessionState.LanguageMode)\r\n", new UTF8Encoding(false));
                var r = Proc.Run(ps, "-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File " + Proc.Q(script), timeoutMs: 120000);
                ScriptsOk = r.Ok && r.Out.Contains("script-ok");
                if (ScriptsOk)
                {
                    var mode = r.Out.Substring(r.Out.IndexOf("script-ok") + 10).Trim();
                    ScriptsDetail = $"a local PowerShell script runs ({mode})";
                    if (mode != "FullLanguage") ScriptsDetail += ", but Scoop needs FullLanguage";
                }
                else
                {
                    var err = r.Err.Split('\n').Select(l => l.Trim()).FirstOrDefault(l => l.Length > 0) ?? $"exit {r.Code}";
                    ScriptsDetail = "local PowerShell scripts can't run: " + err;
                }
            }
            catch (Exception e) { ScriptsOk = false; ScriptsDetail = "couldn't test PowerShell scripts: " + e.Message; }
            finally { try { Directory.Delete(dir, true); } catch { } }
        }

        static string PolicyValue(RegistryKey hive, string path)
        {
            using (var k = hive.OpenSubKey(path))
            {
                if (k == null) return "Undefined";
                var enable = k.GetValue("EnableScripts");
                var value = k.GetValue("ExecutionPolicy") as string;
                if (enable is int i && i == 0) return "Restricted (scripts turned off)";
                return value ?? "Undefined";
            }
        }

        /// <summary>Is Microsoft's linker there? Rust's MSVC toolchain needs it; the GNU one doesn't.</summary>
        void ProbeMsvc()
        {
            var pf86 = Environment.GetFolderPath(Environment.SpecialFolder.ProgramFilesX86);
            var vswhere = Path.Combine(pf86, @"Microsoft Visual Studio\Installer\vswhere.exe");
            string vs = null;
            if (File.Exists(vswhere))
            {
                try
                {
                    var r = Proc.Run(vswhere, "-latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath", timeoutMs: 60000);
                    vs = r.Out.Trim().Split('\n').FirstOrDefault()?.Trim();
                }
                catch { }
            }
            var kits = RegString(Registry.LocalMachine, @"SOFTWARE\Microsoft\Windows Kits\Installed Roots", "KitsRoot10")
                       ?? RegString(Registry.LocalMachine, @"SOFTWARE\WOW6432Node\Microsoft\Windows Kits\Installed Roots", "KitsRoot10");
            bool sdk = kits != null && Directory.Exists(Path.Combine(kits, "Lib"));
            Msvc = !string.IsNullOrEmpty(vs) && sdk;
            MsvcDetail = Msvc ? $"Visual Studio C++ tools at {vs}, Windows SDK at {kits}"
                : !string.IsNullOrEmpty(vs) ? "Visual Studio C++ tools found, but no Windows SDK"
                : "no Visual Studio C++ tools";
        }

        void ProbeWsl()
        {
            var sys = Environment.GetFolderPath(Environment.SpecialFolder.System);
            var wsl = Path.Combine(sys, "wsl.exe");
            if (!File.Exists(wsl)) { WslDetail = "wsl.exe isn't on this machine"; return; }
            try
            {
                var env = new Dictionary<string, string> { ["WSL_UTF8"] = "1" };
                var st = Proc.Run(wsl, "--status", timeoutMs: 120000, encoding: Encoding.UTF8, extraEnv: env);
                if (!st.Ok) { WslDetail = "WSL isn't turned on (wsl --status failed)"; return; }
                var list = Proc.Run(wsl, "--list --quiet", timeoutMs: 120000, encoding: Encoding.UTF8, extraEnv: env);
                WslDistros = list.Out.Replace("\0", "").Split('\n').Select(s => s.Trim()).Where(s => s.Length > 0).ToList();
                Wsl = true;
                WslDetail = WslDistros.Count == 0 ? "WSL is on, no distros yet" : "WSL is on; distros: " + string.Join(", ", WslDistros);
            }
            catch (Exception e) { WslDetail = "couldn't run wsl.exe: " + e.Message; }
        }

        public void Print(Ctx ctx, Kit kit)
        {
            void Row(string label, bool? good, string text)
            {
                var mark = good == null ? "  info " : good.Value ? "  ok   " : "  NO   ";
                var color = good == null ? (ConsoleColor?)null : good.Value ? ConsoleColor.Green : ConsoleColor.Yellow;
                if (color.HasValue) Console.ForegroundColor = color.Value;
                Console.Write(mark);
                Console.ResetColor();
                Log.FileOnly(mark + $"{label,-22}{text}");
                Console.WriteLine($"{label,-22}{text}");
            }

            Log.Head("This machine");
            Row("Windows", Is64, Windows + (Is64 ? ", 64-bit" : ", 32-bit (the kit needs 64-bit)"));
            Row("Programs from", ExeOk, ExeDetail);
            Row("PowerShell scripts", ScriptsOk, ScriptsDetail ?? "");
            Row("PowerShell mode", LanguageMode == "FullLanguage", LanguageMode ?? "n/a");
            foreach (var kv in Policies) Row("  " + kv.Key, null, kv.Value);
            Row("C++ linker (MSVC)", null, MsvcDetail + (Msvc ? " -> Rust uses MSVC" : " -> Rust uses the GNU toolchain"));
            Row("WSL", null, WslDetail);
            Row("Long paths", null, LongPaths ? "on" : "off (dtk works around it; some tools may hit the 260-character limit)");
            Row("Free space", null, FreeBytes < 0 ? "unknown" : $"{Fs.Size(FreeBytes)} on {Path.GetPathRoot(ctx.Root)} (install folder {ctx.Root})");
            foreach (var p in OnPath) Row("Already on PATH", null, p);

            if (kit != null)
            {
                Log.Head($"This release ({kit.Version}, in {kit.Dir})");
                foreach (var g in kit.Groups)
                {
                    var needed = Planner.AssetsFor(kit, g, ScoopPossible ? "scoop" : "portable");
                    var missing = needed.SelectMany(kit.MissingFiles).ToList();
                    var size = needed.Sum(kit.Size);
                    Row(g.Name, missing.Count == 0, missing.Count == 0 ? $"{g.Title} ({Fs.Size(size)})" : $"{g.Title}: missing {string.Join(", ", missing.Take(4))}{(missing.Count > 4 ? ", ..." : "")}");
                }
            }

            Log.Head("Verdict");
            if (!Is64) Log.Error("This kit is for 64-bit Windows.");
            else if (!ExeOk) Log.Error("Programs can't run from your user folders on this machine, so nothing in the kit can. Ask IT whether there's a folder where you're allowed to run tools, and pass it with --root.");
            else if (ScoopPossible) Log.Ok("Install will use Scoop (scripts run here). If a tool fails under Scoop, dtk falls back to its ready-unpacked copy.");
            else Log.Ok("Install will use the ready-unpacked tools (no Scoop; PowerShell scripts can't run here). Everything else is the same.");
            if (!Wsl) Log.Info("The WSL group will be skipped: WSL isn't turned on, and turning it on needs admin.");
        }
    }
}
