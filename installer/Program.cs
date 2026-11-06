using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Text;

namespace DevToolkit
{
    static class Program
    {
        public static string Version =>
            typeof(Program).Assembly.GetCustomAttribute<AssemblyInformationalVersionAttribute>()?.InformationalVersion ?? "dev";

        const string Usage = @"dtk - installs a dev-toolkit release on a machine with no internet and no admin rights.

  dtk check                 What this machine allows, which route the install will take, what's in the release.
  dtk install               Install everything this machine can take. Options:
        --groups a,b          only these groups (core is always included); see `dtk check` for the list
        --skip a,b            everything except these groups
        --root DIR            install folder (default %LOCALAPPDATA%\dev-toolkit)
        --route scoop|portable  force a route instead of choosing from the checks
        --no-wsl              don't import the WSL distro even if WSL is on
        --replace-wsl         re-import the WSL distro if it already exists
        --skip-verify         don't check the files against SHA256SUMS.txt (faster on a re-run)
        --from DIR            the release folder (default: the folder dtk.exe is in)
  dtk verify                Check every release file against SHA256SUMS.txt.
  dtk uninstall             Undo everything install changed and delete the install folder.
        --keep-files          undo the settings but keep the files
        --remove-wsl          also unregister the dev-toolkit WSL distro (deletes its disk)
  dtk exec -- CMD ARGS      Run a command with the environment a new terminal would get.
  dtk version

Logs go to <root>\install.log. Nothing here needs admin.";

        [STAThread]
        static int Main(string[] argv)
        {
            try { Console.OutputEncoding = new UTF8Encoding(false); } catch { }
            try
            {
                var a = new Args(argv);
                switch (a.Command)
                {
                    case "ping": Console.WriteLine("pong"); return 0;
                    case "version": Console.WriteLine("dtk " + Version); return 0;
                    case "check": return Check(a);
                    case "install": return Install(a);
                    case "verify": return VerifyAll(a);
                    case "uninstall": return Uninstall(a);
                    case "exec": return Exec(a);
                    case "help": Console.WriteLine(Usage); return 0;
                    default: Console.WriteLine(Usage); return 2;
                }
            }
            catch (UserError e) { Log.Error(e.Message); return 1; }
            catch (Exception e) { Log.Error(e.ToString()); return 1; }
        }

        static int Check(Args a)
        {
            var ctx = Ctx.Create(a, needKit: false, needRoot: false);
            Kit kit = null;
            if (File.Exists(Path.Combine(ctx.KitDir, "dev-toolkit.json"))) kit = Kit.Load(ctx.KitDir);
            Log.Plain($"dtk {Version}");
            var checks = Checks.Run(ctx);
            checks.Print(ctx, kit);
            return checks.Is64 && checks.ExeOk ? 0 : 3;
        }

        static List<Group> SelectGroups(Ctx ctx, Checks checks)
        {
            var want = ctx.A.List("groups");
            var skip = ctx.A.List("skip");
            foreach (var n in want.Concat(skip))
                if (ctx.Kit.Group(n) == null) throw new UserError($"No group called {n}. Groups in this release: {string.Join(", ", ctx.Kit.Groups.Select(g => g.Name))}");
            var sel = new List<Group>();
            foreach (var g in ctx.Kit.Groups)
            {
                bool include = g.Required || want.Count == 0 || want.Contains(g.Name);
                if (!g.Required && skip.Contains(g.Name)) include = false;
                if (include && g.Name == "wsl")
                {
                    if (ctx.A.Flag("no-wsl")) include = false;
                    else if (!checks.Wsl)
                    {
                        if (want.Contains("wsl")) Log.Warn("Skipping the WSL group: " + checks.WslDetail);
                        include = false;
                    }
                }
                if (include) sel.Add(g);
            }
            return sel;
        }

        static int Install(Args a)
        {
            var sw = Stopwatch.StartNew();
            var ctx = Ctx.Create(a, needKit: true);
            Directory.CreateDirectory(ctx.Root);
            Log.OpenFile(Path.Combine(ctx.Root, "install.log"));
            Log.Plain($"dtk {Version}: installing dev-toolkit {ctx.Kit.Version} into {ctx.Root}");
            var force = a.Flag("force");

            var checks = Checks.Run(ctx);
            checks.Print(ctx, ctx.Kit);
            if (!checks.Is64) throw new UserError("This kit is for 64-bit Windows.");
            if (!checks.ExeOk && !force) throw new UserError("Programs can't run from your user folders here, so the tools wouldn't run either. Nothing was installed.");

            var route = (a.Get("route") ?? "auto").ToLowerInvariant();
            if (route == "auto") route = checks.ScoopPossible && ctx.Kit.Declares("scoop") ? "scoop" : "portable";
            if (route != "scoop" && route != "portable") throw new UserError("--route must be scoop or portable");
            if (route == "scoop" && ctx.P.Sandbox) throw new UserError("--home can't be combined with the Scoop route (Scoop writes your real user settings). Add --route portable.");
            if (route == "scoop" && !checks.ScoopPossible && !force) throw new UserError("The Scoop route needs PowerShell scripts to run, and they can't here: " + checks.ScriptsDetail);
            ctx.Route = route;

            var groups = SelectGroups(ctx, checks);
            Log.Head($"Plan: {route} route; groups {string.Join(", ", groups.Select(g => g.Name))}");
            var needed = groups.SelectMany(g => Planner.AssetsFor(ctx.Kit, g, route)).Distinct().ToList();
            var missing = needed.SelectMany(ctx.Kit.MissingFiles).ToList();
            if (missing.Count > 0)
                throw new UserError("These release files aren't in " + ctx.Kit.Dir + ":\n     " + string.Join("\n     ", missing) +
                                    "\n   Copy them over, or leave their groups out with --skip.");

            long size = needed.Sum(ctx.Kit.Size);
            long want = (long)(size * 2.5);
            Log.Info($"release files for this install: {Fs.Size(size)}; expect about {Fs.Size(want)} installed");
            if (checks.FreeBytes >= 0 && checks.FreeBytes < want && !force)
                throw new UserError($"Not enough space: about {Fs.Size(want)} needed, {Fs.Size(checks.FreeBytes)} free on {Path.GetPathRoot(ctx.Root)}. Free some up, pick another drive with --root, or install fewer groups.");

            foreach (var opt in Planner.OptionalFor(groups))
            {
                if (ctx.Kit.Has(opt)) needed.Add(opt);
                else Log.Warn($"{opt} isn't here, so that part is skipped ({string.Join(", ", ctx.Kit.MissingFiles(opt))})");
            }

            var verified = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            if (!a.Flag("skip-verify"))
            {
                Log.Head("Checking the files against SHA256SUMS.txt");
                ctx.Kit.Verify(needed);
                foreach (var n in needed) verified.Add(n);
            }

            ctx.Rec.Set("version", ctx.Kit.Version);
            ctx.Rec.Set("root", ctx.Root);
            ctx.Rec.Set("route", route);
            ctx.Rec.Set("groups", groups.Select(g => g.Name).ToArray());
            ctx.Rec.Set("installed", DateTime.Now.ToString("o"));
            ctx.Env.SetVar("DEV_TOOLKIT_ROOT", ctx.Root);
            ctx.Rec.Save();

            try
            {
                var withApps = groups.Where(g => g.Apps.Count > 0).ToList();
                if (route == "scoop")
                {
                    var failed = ScoopRoute.Install(ctx, withApps, checks);
                    if (failed.Count > 0)
                    {
                        var fb = withApps.Where(g => g.AppNames.Any(n => failed.Contains(n, StringComparer.OrdinalIgnoreCase))).ToList();
                        var fbAssets = fb.Select(g => $"tools-{g.Name}-portable").Where(x => !verified.Contains(x)).ToList();
                        var gone = fbAssets.SelectMany(ctx.Kit.MissingFiles).ToList();
                        if (gone.Count > 0) ctx.Problems.Add($"{string.Join(", ", failed)} didn't install, and the ready-unpacked copies aren't here ({string.Join(", ", gone)})");
                        else
                        {
                            if (fbAssets.Count > 0 && !a.Flag("skip-verify")) ctx.Kit.Verify(fbAssets);
                            PortableRoute.Install(ctx, fb, failed);
                        }
                    }
                }
                else PortableRoute.Install(ctx, withApps);
                ctx.Rec.Save();

                foreach (var g in groups)
                {
                    if (g.Assets.Contains("rust-toolchain")) Languages.Rust(ctx);
                    if (g.Assets.Contains("python")) Languages.Python(ctx);
                    if (g.Assets.Contains("npm-cache")) Languages.Node(ctx);
                    if (g.Assets.Contains("nuget")) Languages.DotNet(ctx);
                    ctx.Rec.Save();
                }
                Languages.VsCode(ctx, groups);
                if (groups.Any(g => g.Name == "wsl")) Languages.Wsl(ctx, checks);
            }
            finally
            {
                ctx.Env.Broadcast();
                ctx.Rec.Save();
            }

            Log.Head($"Done in {sw.Elapsed.TotalMinutes:0.0} minutes");
            foreach (var p in ctx.Problems) Log.Warn(p);
            Log.Info("Open a NEW terminal so it picks up the new PATH. Tools that are already open keep the old one.");
            Log.Info($"Everything is in {ctx.Root}; settings changed are listed in installed.json. Undo with: dtk uninstall");
            return ctx.Problems.Count == 0 ? 0 : 4;
        }

        static int VerifyAll(Args a)
        {
            var ctx = Ctx.Create(a, needKit: true, needRoot: false);
            var present = ctx.Kit.Groups.SelectMany(g => Planner.AssetsFor(ctx.Kit, g, "scoop").Concat(Planner.AssetsFor(ctx.Kit, g, "portable")))
                .Distinct().Where(ctx.Kit.Declares).ToList();
            var missing = present.SelectMany(ctx.Kit.MissingFiles).ToList();
            ctx.Kit.Verify(present.Where(ctx.Kit.Has));
            foreach (var m in missing) Log.Warn("not here: " + m);
            return missing.Count == 0 ? 0 : 1;
        }

        static int Uninstall(Args a)
        {
            var ctx = Ctx.Create(a, needKit: false);
            if (!File.Exists(ctx.Rec.FilePath)) throw new UserError($"Nothing to undo: {ctx.Rec.FilePath} doesn't exist.");
            Log.OpenFile(Path.Combine(ctx.P.LocalAppData, "dev-toolkit-uninstall.log"));
            Log.Head($"Undoing the dev-toolkit install in {ctx.Root}");

            foreach (var kv in ctx.Rec.Vars)
            {
                if (kv.Value == null) ctx.Env.Store.Delete(kv.Key); else ctx.Env.Store.Set(kv.Key, Convert.ToString(kv.Value));
                Log.Info($"variable {kv.Key} {(kv.Value == null ? "removed" : "restored")}");
            }
            foreach (var p in ctx.Rec.PathEntries) { ctx.Env.RemovePath(p); Log.Info("PATH -= " + p); }
            foreach (var kv in ctx.Rec.Files)
            {
                var backup = kv.Value as string;
                try
                {
                    if (backup != null && File.Exists(backup)) { File.Copy(backup, kv.Key, true); File.Delete(backup); Log.Info("restored " + kv.Key); }
                    else if (backup == null && File.Exists(kv.Key)) { File.Delete(kv.Key); Log.Info("removed " + kv.Key); }
                }
                catch (Exception e) { Log.Warn($"{kv.Key}: {e.Message}"); }
            }
            foreach (var lnk in ctx.Rec.Items("shortcuts")) try { File.Delete(lnk); } catch { }
            try { Directory.Delete(Path.Combine(ctx.P.StartMenuPrograms, "dev-toolkit"), true); } catch { }
            foreach (var f in ctx.Rec.Items("fonts"))
            {
                var parts = f.Split('|');
                try
                {
                    if (!ctx.P.Sandbox)
                    {
                        using (var k = Microsoft.Win32.Registry.CurrentUser.OpenSubKey(@"Software\Microsoft\Windows NT\CurrentVersion\Fonts", true))
                            k?.DeleteValue(parts[0], false);
                        Native.RemoveFontResource(parts[1]);
                    }
                    File.Delete(parts[1]);
                }
                catch { }
            }
            var distro = ctx.Rec.Data.Str("wslDistro");
            if (distro != null)
            {
                if (a.Flag("remove-wsl"))
                {
                    var wsl = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System), "wsl.exe");
                    var r = Proc.Run(wsl, "--unregister " + distro, timeoutMs: 600000, encoding: Encoding.UTF8, extraEnv: new Dictionary<string, string> { ["WSL_UTF8"] = "1" });
                    Log.Info(r.Ok ? $"WSL distro {distro} unregistered" : $"wsl --unregister {distro} failed: {r.Tail(3)}");
                }
                else Log.Info($"The WSL distro {distro} stays (its disk is under {ctx.Root}\\wsl). Remove it with --remove-wsl.");
            }
            ctx.Env.Broadcast();

            if (a.Flag("keep-files") || (distro != null && !a.Flag("remove-wsl")))
            {
                File.Delete(ctx.Rec.FilePath);
                Log.Info($"Files kept in {ctx.Root}");
            }
            else
            {
                var r = ctx.Root;
                var unsafeRoots = new[] { ctx.P.UserProfile, ctx.P.LocalAppData, ctx.P.AppData, Path.GetPathRoot(r) }
                    .Select(x => x.TrimEnd('\\')).ToList();
                if (unsafeRoots.Any(x => x.Equals(r.TrimEnd('\\'), StringComparison.OrdinalIgnoreCase)))
                    throw new UserError($"Refusing to delete {r}; delete what you don't want by hand.");
                Log.Info("deleting " + r);
                Fs.DeleteTree(r);
            }
            Log.Head("Done. Open a new terminal to get the old PATH back.");
            return 0;
        }

        /// <summary>Runs a command with the environment a new terminal would get (tests use this).</summary>
        static int Exec(Args a)
        {
            var ctx = Ctx.Create(a, needKit: false, needRoot: false);
            var env = UserEnv.Fresh(ctx);
            if (a.Rest.Count == 0)
            {
                foreach (var kv in env.OrderBy(k => k.Key)) Console.WriteLine($"{kv.Key}={kv.Value}");
                return 0;
            }
            var exe = Resolve(a.Rest[0], env["Path"]) ?? throw new UserError($"{a.Rest[0]} isn't on the PATH");
            var args = string.Join(" ", a.Rest.Skip(1).Select(Proc.Q));
            var psi = new ProcessStartInfo(exe, args) { UseShellExecute = false };
            var ext = Path.GetExtension(exe).ToLowerInvariant();
            if (ext == ".cmd" || ext == ".bat")
                psi = new ProcessStartInfo(Environment.GetEnvironmentVariable("ComSpec") ?? "cmd.exe", $"/d /s /c \"\"{exe}\" {args}\"") { UseShellExecute = false };
            psi.EnvironmentVariables.Clear();
            foreach (var kv in env) psi.EnvironmentVariables[kv.Key] = kv.Value;
            using (var p = Process.Start(psi)) { p.WaitForExit(); return p.ExitCode; }
        }

        static string Resolve(string cmd, string path)
        {
            if (Path.IsPathRooted(cmd) && File.Exists(cmd)) return cmd;
            var exts = Path.HasExtension(cmd) ? new[] { "" } : new[] { ".exe", ".cmd", ".bat", ".com" };
            foreach (var dir in UserEnv.SplitPath(path))
                foreach (var e in exts)
                {
                    try
                    {
                        var p = Path.Combine(dir, cmd + e);
                        if (File.Exists(p)) return p;
                    }
                    catch { }
                }
            return null;
        }
    }
}
