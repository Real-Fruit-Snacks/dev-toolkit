using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text;
using System.Text.RegularExpressions;

namespace DevToolkit
{
    /// <summary>Which release assets a group needs on each route.</summary>
    static class Planner
    {
        public static List<string> AssetsFor(Kit kit, Group g, string route)
        {
            var list = new List<string>();
            if (g.Apps.Count > 0)
            {
                if (route == "scoop") { list.Add("scoop"); list.Add($"tools-{g.Name}-scoop"); }
                else list.Add($"tools-{g.Name}-portable");
            }
            list.AddRange(g.Assets);
            if (g.Required && route == "scoop") list.Add($"tools-{g.Name}-portable"); // shim.exe and the per-app fallback
            return list.Distinct().ToList();
        }

        /// <summary>Assets that are used when present but don't stop an install when missing.</summary>
        public static List<string> OptionalFor(IEnumerable<Group> groups) =>
            groups.Any(g => g.Extensions.Count > 0) ? new List<string> { "vscode-extensions" } : new List<string>();
    }

    /// <summary>
    /// Scoop route: Scoop itself, its buckets at the pinned commit, and its download cache already filled,
    /// so `scoop install` finds every file locally and never reaches for the network.
    /// </summary>
    static class ScoopRoute
    {
        public static string Dir(Ctx ctx) => Path.Combine(ctx.Root, "scoop");

        public static List<string> Install(Ctx ctx, List<Group> groups, Checks checks)
        {
            var scoop = Dir(ctx);
            Log.Head("Scoop");
            ctx.Kit.Extract("scoop", scoop, name =>
                name.StartsWith("core/") ? "apps/scoop/current/" + name.Substring(5)
                : name.StartsWith("buckets/") ? name : null);
            foreach (var g in groups.Where(g => g.Apps.Count > 0))
                ctx.Kit.Extract($"tools-{g.Name}-scoop", Path.Combine(scoop, "cache"));

            WriteShims(scoop);
            WriteConfig(ctx, scoop);

            // Scoop sets PATH and variables itself; compare before and after so uninstall can undo them.
            var before = ctx.Env.Store.All();
            ctx.Env.SetVar("SCOOP", scoop);
            ctx.Env.AddPath(Path.Combine(scoop, "shims"));

            var apps = groups.SelectMany(g => g.Apps).Distinct(StringComparer.OrdinalIgnoreCase).ToList();
            Log.Info($"scoop install {apps.Count} apps (from the local cache)");
            var script = Path.Combine(scoop, @"apps\scoop\current\bin\scoop.ps1");
            var r = Proc.Run(checks.PowerShell,
                "-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File " + Proc.Q(script) +
                " install --no-update-scoop " + string.Join(" ", apps.Select(Proc.Q)),
                ctx, timeoutMs: 3 * 60 * 60 * 1000, echo: true);
            ctx.Env.RecordDiff(before);

            var failed = new List<string>();
            foreach (var id in apps)
            {
                var name = id.Substring(id.IndexOf('/') + 1);
                var dir = Path.Combine(scoop, "apps", name, "current");
                var ok = new[] { "scoop-install.json", "install.json", "scoop-manifest.json", "manifest.json" }.Any(f => File.Exists(Path.Combine(dir, f)));
                if (ok) { ctx.AppDirs[name] = dir; Log.Ok(name); }
                else { failed.Add(name); Log.Warn($"{name} didn't install through Scoop"); }
            }
            if (r.Code != 0 && failed.Count == 0) Log.Warn($"scoop exited with {r.Code}, but every app is in place");
            return failed;
        }

        /// <summary>The scoop command shims, written the way Scoop's own installer writes them.</summary>
        static void WriteShims(string scoop)
        {
            var shims = Path.Combine(scoop, "shims");
            Directory.CreateDirectory(shims);
            var target = Path.Combine(scoop, @"apps\scoop\current\bin\scoop.ps1");
            Fs.WriteText(Path.Combine(shims, "scoop.ps1"), string.Join("\r\n",
                $"# {target}",
                "$path = Join-Path $PSScriptRoot \"..\\apps\\scoop\\current\\bin\\scoop.ps1\"",
                "if ($MyInvocation.ExpectingInput) { $input | & $path @args } else { & $path @args }",
                "exit $LASTEXITCODE", ""));
            Fs.WriteText(Path.Combine(shims, "scoop.cmd"), string.Join("\r\n",
                $"@rem {target}",
                "@echo off",
                "setlocal enabledelayedexpansion",
                "set args=%*",
                ":: replace problem characters in arguments",
                "set args=%args:\"='%",
                "set args=%args:(=`(%",
                "set args=%args:)=`)%",
                "set invalid=\"='",
                "if !args! == !invalid! ( set args= )",
                "where /q pwsh.exe",
                "if %errorlevel% equ 0 (",
                $"    pwsh -noprofile -ex unrestricted -file \"{target}\" %args%",
                ") else (",
                $"    powershell -noprofile -ex unrestricted -file \"{target}\" %args%",
                ")", ""));
            var unix = target.Replace('\\', '/');
            Fs.WriteText(Path.Combine(shims, "scoop"), string.Join("\n",
                "#!/bin/sh",
                $"# {target}",
                "if command -v pwsh.exe > /dev/null 2>&1; then",
                $"    pwsh.exe -noprofile -ex unrestricted -file \"{target}\" \"$@\"",
                "else",
                $"    powershell.exe -noprofile -ex unrestricted -file \"{target}\" \"$@\"",
                "fi", ""));
        }

        /// <summary>Scoop's config: our root, and a fresh last_update so it doesn't try to update itself.</summary>
        static void WriteConfig(Ctx ctx, string scoop)
        {
            var xdg = Environment.GetEnvironmentVariable("XDG_CONFIG_HOME");
            var dir = !ctx.P.Sandbox && !string.IsNullOrEmpty(xdg) ? Path.Combine(xdg, "scoop") : Path.Combine(ctx.P.UserProfile, ".config", "scoop");
            var path = Path.Combine(dir, "config.json");
            ConfigFiles.Backup(ctx, path);
            var cfg = File.Exists(path) ? Json.ReadObject(path) : new Dictionary<string, object>();
            cfg["root_path"] = scoop;
            cfg["last_update"] = DateTime.Now.ToString("o");
            cfg["aria2-enabled"] = false;
            cfg["show_update_log"] = false;
            Fs.WriteText(path, Json.Write(cfg));
        }
    }

    /// <summary>
    /// Portable route: tools already unpacked by Scoop on the build machine. dtk copies them in, fixes the
    /// paths they recorded, and makes shims, PATH entries, variables, shortcuts and fonts, with no scripts at all.
    /// </summary>
    static class PortableRoute
    {
        public static string Dir(Ctx ctx) => Path.Combine(ctx.Root, "portable");

        public static void Install(Ctx ctx, List<Group> groups, ICollection<string> onlyApps = null)
        {
            var root = Dir(ctx);
            var shims = Path.Combine(root, "shims");
            Directory.CreateDirectory(shims);
            ctx.Env.AddPath(shims);

            // shim.exe (from Scoop) ships with the core group
            var shimExe = Path.Combine(shims, "shim.exe");
            if (!File.Exists(shimExe))
            {
                var bytes = ctx.Kit.ReadEntry("tools-core-portable", "shims/shim.exe");
                if (bytes == null) throw new UserError("tools-core-portable.zip has no shims/shim.exe");
                File.WriteAllBytes(shimExe, bytes);
            }

            foreach (var g in groups.Where(g => g.Apps.Count > 0))
            {
                var asset = $"tools-{g.Name}-portable";
                var meta = Json.ParseObject(Encoding.UTF8.GetString(ctx.Kit.ReadEntry(asset, "portable.json") ?? throw new UserError($"{asset} has no portable.json")));
                var apps = meta.Arr("apps").Cast<Dictionary<string, object>>().ToList();
                if (onlyApps != null) apps = apps.Where(a => onlyApps.Contains(a.Str("name"))).ToList();
                if (apps.Count == 0) continue;
                var wanted = new HashSet<string>(apps.Select(a => a.Str("name")), StringComparer.OrdinalIgnoreCase);

                Log.Head($"Ready-unpacked tools: {g.Name}" + (onlyApps != null ? " (fallback for " + string.Join(", ", wanted) + ")" : ""));
                ctx.Kit.Extract(asset, root, name =>
                {
                    if (!name.StartsWith("apps/")) return null;
                    var app = name.Substring(5).Split('/')[0];
                    return wanted.Contains(app) ? name : null;
                });
                foreach (var a in apps) SetUpApp(ctx, a, root, shims, shimExe);
            }
        }

        static void SetUpApp(Ctx ctx, Dictionary<string, object> a, string root, string shims, string shimExe)
        {
            var name = a.Str("name");
            var dir = Path.Combine(root, "apps", name);
            ctx.AppDirs[name] = dir;
            string Expand(string s) => s.Replace("{app}", dir).Replace("{root}", ctx.Root);

            // paths the build machine wrote into config files
            foreach (var rel in a.Strs("rewrite"))
            {
                var f = Path.Combine(dir, rel.Replace('/', '\\'));
                if (!File.Exists(f)) continue;
                var text = File.ReadAllText(f);
                var orig = text;
                // the build machine's folders for this app, longest first so a longer root is replaced before a prefix of it
                foreach (var br in a.Strs("roots").OrderByDescending(r => r.Length))
                {
                    foreach (var (from, to) in new[]
                    {
                        (br, dir),
                        (br.Replace("\\", "\\\\"), dir.Replace("\\", "\\\\")),
                        (br.Replace('\\', '/'), dir.Replace('\\', '/')),
                    })
                        text = Regex.Replace(text, Regex.Escape(from), to.Replace("$", "$$"), RegexOptions.IgnoreCase);
                }
                if (text != orig) File.WriteAllText(f, text, new UTF8Encoding(false));
            }

            foreach (var b in a.Arr("bins").Cast<Dictionary<string, object>>())
            {
                var target = Path.Combine(dir, b.Str("path").Replace('/', '\\'));
                var alias = b.Str("alias");
                if (string.IsNullOrEmpty(alias)) alias = Path.GetFileNameWithoutExtension(target);
                var args = Expand(b.Str("args") ?? "");
                WriteShim(shims, shimExe, alias, target, args);
            }
            foreach (var p in a.Strs("paths")) ctx.Env.AddPath(Path.GetFullPath(Path.Combine(dir, p.Replace('/', '\\'))));
            var env = a.Obj("env");
            if (env != null) foreach (var kv in env) ctx.Env.SetVar(kv.Key, Expand(Convert.ToString(kv.Value)));

            foreach (var s in a.Arr("shortcuts").Cast<Dictionary<string, object>>())
            {
                try
                {
                    var lnk = Path.Combine(ctx.P.StartMenuPrograms, "dev-toolkit", s.Str("name").Replace('/', '\\') + ".lnk");
                    Native.CreateShortcut(lnk, Path.Combine(dir, s.Str("target").Replace('/', '\\')), Expand(s.Str("args") ?? ""), null);
                    ctx.Rec.NoteItem("shortcuts", lnk);
                }
                catch (Exception e) { Log.Warn($"shortcut {s.Str("name")}: {e.Message}"); }
            }
            foreach (var font in a.Strs("fonts")) Fonts.Install(ctx, Path.Combine(dir, font.Replace('/', '\\')));
            Log.Ok($"{name} {a.Str("version")}");
        }

        public static void WriteShim(string shims, string shimExe, string alias, string target, string args)
        {
            var ext = Path.GetExtension(target).ToLowerInvariant();
            foreach (var old in new[] { ".exe", ".shim", ".cmd" })
            {
                var p = Path.Combine(shims, alias + old);
                if (File.Exists(p)) File.Delete(p);
            }
            if (ext == ".exe" || ext == ".com")
            {
                File.Copy(shimExe, Path.Combine(shims, alias + ".exe"), true);
                if (Pe.Subsystem(target) == 2) Pe.SetSubsystem(Path.Combine(shims, alias + ".exe"), 2); // GUI target: no console window
                var sb = new StringBuilder($"path = \"{target}\"\r\n");
                if (!string.IsNullOrEmpty(args)) sb.Append($"args = {args}\r\n");
                File.WriteAllText(Path.Combine(shims, alias + ".shim"), sb.ToString(), new UTF8Encoding(false));
            }
            else if (ext == ".cmd" || ext == ".bat")
            {
                File.WriteAllText(Path.Combine(shims, alias + ".cmd"), $"@\"{target}\" {args} %*\r\n", Encoding.Default);
            }
            else if (ext == ".ps1")
            {
                File.WriteAllText(Path.Combine(shims, alias + ".cmd"),
                    $"@powershell -NoLogo -NoProfile -ExecutionPolicy Bypass -File \"{target}\" {args} %*\r\n", Encoding.Default);
            }
            else if (ext == ".jar")
            {
                File.WriteAllText(Path.Combine(shims, alias + ".cmd"), $"@java -jar \"{target}\" {args} %*\r\n", Encoding.Default);
            }
            else
            {
                File.WriteAllText(Path.Combine(shims, alias + ".cmd"), $"@\"{target}\" {args} %*\r\n", Encoding.Default);
            }
        }
    }

    /// <summary>Reads and sets a program's PE subsystem (2 = GUI, 3 = console), as Scoop does for its shims.</summary>
    static class Pe
    {
        static long SubsystemOffset(BinaryReader r)
        {
            r.BaseStream.Position = 0x3C;
            var peHeader = r.ReadInt32();
            r.BaseStream.Position = peHeader;
            if (r.ReadUInt32() != 0x00004550) return -1; // "PE\0\0"
            return peHeader + 4 + 20 + 68;               // signature, file header, then Subsystem in the optional header
        }

        public static int Subsystem(string exe)
        {
            try
            {
                using (var r = new BinaryReader(File.OpenRead(exe)))
                {
                    var off = SubsystemOffset(r);
                    if (off < 0) return 0;
                    r.BaseStream.Position = off;
                    return r.ReadUInt16();
                }
            }
            catch { return 0; }
        }

        public static void SetSubsystem(string exe, ushort value)
        {
            using (var fs = new FileStream(exe, FileMode.Open, FileAccess.ReadWrite))
            {
                var r = new BinaryReader(fs);
                var off = SubsystemOffset(r);
                if (off < 0) return;
                fs.Position = off;
                new BinaryWriter(fs).Write(value);
            }
        }
    }

    static class Fonts
    {
        /// <summary>Per-user font install (Windows 10 1809 and later): copy, register in HKCU, load.</summary>
        public static void Install(Ctx ctx, string file)
        {
            if (!File.Exists(file)) return;
            var dest = Path.Combine(ctx.P.FontsDir, Path.GetFileName(file));
            Directory.CreateDirectory(ctx.P.FontsDir);
            try { File.Copy(file, dest, true); }
            catch (IOException) { return; } // in use: already installed and loaded
            var label = Path.GetFileNameWithoutExtension(file) + (file.EndsWith(".otf", StringComparison.OrdinalIgnoreCase) ? " (OpenType)" : " (TrueType)");
            if (!ctx.P.Sandbox)
            {
                using (var k = Microsoft.Win32.Registry.CurrentUser.CreateSubKey(@"Software\Microsoft\Windows NT\CurrentVersion\Fonts"))
                    k.SetValue(label, dest);
                Native.AddFontResource(dest);
            }
            ctx.Rec.NoteItem("fonts", label + "|" + dest);
        }
    }
}
