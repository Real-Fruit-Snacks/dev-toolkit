using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text;

namespace DevToolkit
{
    /// <summary>The parts dtk builds itself: toolchains, package caches and the config that points tools at them.</summary>
    static class Languages
    {
        public static void Rust(Ctx ctx)
        {
            Log.Head("Rust");
            var rust = Path.Combine(ctx.Root, "rust");
            ctx.Kit.Extract("rust-toolchain", rust);
            var crates = Path.Combine(ctx.Root, "crates");
            ctx.Kit.Extract("crates", crates);

            var cargoHome = Path.Combine(rust, "cargo");
            var rustupHome = Path.Combine(rust, "rustup");
            ctx.Env.SetVar("CARGO_HOME", cargoHome);
            ctx.Env.SetVar("RUSTUP_HOME", rustupHome);
            ctx.Env.AddPath(Path.Combine(cargoHome, "bin"));

            var cfg = new StringBuilder();
            cfg.AppendLine("# Written by dev-toolkit (dtk.exe). Cargo reads crates from the local registry and never");
            cfg.AppendLine("# goes online. Re-running dtk.exe install rewrites this file.");
            cfg.AppendLine();
            cfg.AppendLine("[source.crates-io]");
            cfg.AppendLine("replace-with = 'dev-toolkit'");
            cfg.AppendLine();
            cfg.AppendLine("[source.dev-toolkit]");
            cfg.AppendLine("local-registry = " + ConfigFiles.TomlLit(crates));
            cfg.AppendLine();
            cfg.AppendLine("[net]");
            cfg.AppendLine("offline = true");
            ConfigFiles.WriteOwned(ctx, Path.Combine(cargoHome, "config.toml"), cfg.ToString());

            // MSVC if Microsoft's linker works here, GNU (which links on its own) if not.
            var rustup = Path.Combine(cargoHome, "bin", "rustup.exe");
            var toolchain = MsvcLinks(ctx, cargoHome) ? "stable-x86_64-pc-windows-msvc" : "stable-x86_64-pc-windows-gnu";
            var r = Proc.Run(rustup, "default " + toolchain, ctx, timeoutMs: 300000);
            if (!r.Ok) ctx.Problems.Add($"rustup default {toolchain} failed: {r.Tail(5)}");
            var v = Proc.Run(Path.Combine(cargoHome, "bin", "rustc.exe"), "--version", ctx, timeoutMs: 120000);
            if (v.Ok) Log.Ok($"{v.Out.Trim()} (default toolchain {toolchain})");
            else ctx.Problems.Add("rustc --version failed: " + v.Tail(5));
            ctx.Rec.Set("rustToolchain", toolchain);
        }

        /// <summary>Compiles an empty program with the MSVC toolchain: the only reliable way to know the linker works.</summary>
        static bool MsvcLinks(Ctx ctx, string cargoHome)
        {
            var dir = Path.Combine(Path.GetTempPath(), "dev-toolkit-msvc-" + Guid.NewGuid().ToString("N").Substring(0, 8));
            try
            {
                Directory.CreateDirectory(dir);
                File.WriteAllText(Path.Combine(dir, "probe.rs"), "fn main() {}\n");
                var r = Proc.Run(Path.Combine(cargoHome, "bin", "rustc.exe"), "+stable-x86_64-pc-windows-msvc probe.rs -o probe.exe", ctx, dir, 300000);
                var ok = r.Ok && File.Exists(Path.Combine(dir, "probe.exe"));
                Log.Info(ok ? "Microsoft's linker works: Rust will use the MSVC toolchain"
                            : "no working Microsoft linker: Rust will use the GNU toolchain");
                return ok;
            }
            catch { return false; }
            finally { try { Directory.Delete(dir, true); } catch { } }
        }

        public static void Python(Ctx ctx)
        {
            Log.Head("Python");
            var root = Path.Combine(ctx.Root, "python");
            ctx.Kit.Extract("python", root);
            var build = Directory.GetDirectories(root, "cpython-*").OrderByDescending(d => d, StringComparer.OrdinalIgnoreCase).FirstOrDefault()
                        ?? throw new UserError("python.zip has no cpython-* folder");
            ctx.Env.AddPath(Path.Combine(build, "Scripts"));
            ctx.Env.AddPath(build);

            var wheels = Path.Combine(ctx.Root, "wheels", "windows");
            ctx.Kit.Extract("wheels-windows", wheels);

            ConfigFiles.SetIni(ctx, Path.Combine(ctx.P.AppData, "pip", "pip.ini"), "global", new Dictionary<string, string>
            {
                ["no-index"] = "true",
                ["find-links"] = wheels,
                ["disable-pip-version-check"] = "true",
            });
            ConfigFiles.SetTomlTopLevel(ctx, Path.Combine(ctx.P.AppData, "uv", "uv.toml"), new Dictionary<string, string>
            {
                ["offline"] = "true",
                ["no-index"] = "true",
                ["find-links"] = "[" + ConfigFiles.TomlLit(wheels) + "]",
                ["python-downloads"] = "\"never\"",
            });
            ctx.Env.SetVar("UV_OFFLINE", "1");
            ctx.Env.SetVar("UV_PYTHON_DOWNLOADS", "never");
            ctx.AppDirs["python"] = build;

            var v = Proc.Run(Path.Combine(build, "python.exe"), "--version", ctx, timeoutMs: 120000);
            if (v.Ok) Log.Ok($"{v.Out.Trim()}, {Directory.GetFiles(wheels, "*.whl").Length:N0} wheels");
            else ctx.Problems.Add("python --version failed: " + v.Tail(5));
        }

        public static void Node(Ctx ctx)
        {
            Log.Head("Node.js packages");
            var cache = Path.Combine(ctx.Root, "npm-cache");
            ctx.Kit.Extract("npm-cache", cache);
            ConfigFiles.SetKeyValues(ctx, Path.Combine(ctx.P.UserProfile, ".npmrc"), new Dictionary<string, string>
            {
                ["cache"] = cache,
                ["offline"] = "true",
                ["audit"] = "false",
                ["fund"] = "false",
                ["update-notifier"] = "false",
            });
            Log.Ok($"npm uses the cache at {cache}, offline");
        }

        public static void DotNet(Ctx ctx)
        {
            Log.Head(".NET packages");
            var feed = Path.Combine(ctx.Root, "nuget");
            ctx.Kit.Extract("nuget", feed);
            ConfigFiles.NuGetFeed(ctx, Path.Combine(ctx.P.AppData, "NuGet", "NuGet.Config"), "dev-toolkit", feed);
            foreach (var kv in new Dictionary<string, string>
            {
                ["DOTNET_CLI_TELEMETRY_OPTOUT"] = "1",
                ["DOTNET_NOLOGO"] = "1",
                ["DOTNET_SKIP_FIRST_TIME_EXPERIENCE"] = "1",
                ["DOTNET_CLI_WORKLOAD_UPDATE_NOTIFY_DISABLE"] = "1",
                ["NUGET_CERT_REVOCATION_MODE"] = "offline",
            }) ctx.Env.SetVar(kv.Key, kv.Value);
            Log.Ok($"NuGet uses the feed at {feed}; nuget.org is turned off");
        }

        /// <summary>Settings that keep VS Code and the bundled extensions from reaching for the network.</summary>
        static Dictionary<string, object> VsCodeOfflineSettings(Ctx ctx)
        {
            var s = new Dictionary<string, object>
            {
                ["update.mode"] = "none",
                ["update.showReleaseNotes"] = false,
                ["extensions.autoUpdate"] = false,
                ["extensions.autoCheckUpdates"] = false,
                ["extensions.ignoreRecommendations"] = true,
                ["telemetry.telemetryLevel"] = "off",
                ["workbench.enableExperiments"] = false,
                ["workbench.settings.enableNaturalLanguageSearch"] = false,
                ["npm.fetchOnlinePackageInfo"] = false,
                ["json.schemaDownload.enable"] = false,
                ["redhat.telemetry.enabled"] = false,
                ["yaml.schemaStore.enable"] = false,
                ["clangd.checkUpdates"] = false,
                ["clangd.onConfigChanged"] = "restart",
            };
            if (ctx.AppDirs.TryGetValue("python", out var py)) s["python.defaultInterpreterPath"] = Path.Combine(py, "python.exe");
            var llvm = ctx.AppDir("mingw-mstorsjo-llvm-ucrt");
            if (llvm != null && File.Exists(Path.Combine(llvm, "bin", "clangd.exe"))) s["clangd.path"] = Path.Combine(llvm, "bin", "clangd.exe");
            var dotnet = ctx.AppDir("dotnet-sdk-lts");
            if (dotnet != null && File.Exists(Path.Combine(dotnet, "dotnet.exe")))
            {
                s["dotnetAcquisitionExtension.existingDotnetPath"] = new object[]
                {
                    new Dictionary<string, object> { ["extensionId"] = "muhammad-sammy.csharp", ["path"] = Path.Combine(dotnet, "dotnet.exe") },
                };
            }
            return s;
        }

        public static void VsCode(Ctx ctx, List<Group> groups)
        {
            var vscode = ctx.AppDir("vscode");
            if (vscode == null) { Log.Warn("VS Code isn't installed; skipping its settings and extensions"); return; }
            Log.Head("VS Code");
            var settings = Path.Combine(vscode, "data", "user-data", "User", "settings.json");
            ConfigFiles.VsCodeSettings(ctx, settings, VsCodeOfflineSettings(ctx));

            var exts = groups.Where(g => g.Extensions.Count > 0).ToList();
            if (exts.Count == 0 || !ctx.Kit.Has("vscode-extensions")) return;
            var tmp = Path.Combine(ctx.Root, "vscode-extensions");
            ctx.Kit.Extract("vscode-extensions", tmp);
            var order = Json.ReadObject(Path.Combine(tmp, "extensions.json"));
            var code = Path.Combine(vscode, "bin", "code.cmd");
            foreach (var g in exts)
            {
                foreach (var file in order.Strs(g.Name))
                {
                    var vsix = Path.Combine(tmp, g.Name, file);
                    if (!File.Exists(vsix)) vsix = Path.Combine(tmp, file);
                    var r = Proc.RunCmd(code, $"--install-extension {Proc.Q(vsix)} --force", ctx, timeoutMs: 600000);
                    if (r.Ok) Log.Ok(file);
                    else { Log.Warn($"{file}: {r.Tail(3)}"); ctx.Problems.Add($"VS Code extension {file} didn't install"); }
                }
            }
            try { Fs.DeleteTree(tmp); } catch { }
        }

        public static void Wsl(Ctx ctx, Checks checks)
        {
            Log.Head("WSL");
            const string name = "dev-toolkit";
            var sys = Environment.GetFolderPath(Environment.SpecialFolder.System);
            var wsl = Path.Combine(sys, "wsl.exe");
            var env = new Dictionary<string, string> { ["WSL_UTF8"] = "1" };
            var exists = checks.WslDistros.Any(d => d.Equals(name, StringComparison.OrdinalIgnoreCase));

            var wheels = Path.Combine(ctx.Root, "wheels", "linux");
            ctx.Kit.Extract("wheels-linux", wheels);

            if (exists && ctx.A.Flag("replace-wsl"))
            {
                Log.Info($"replacing the existing {name} distro");
                Proc.Run(wsl, $"--unregister {name}", timeoutMs: 600000, encoding: Encoding.UTF8, extraEnv: env);
                exists = false;
            }
            if (!exists)
            {
                var disk = Path.Combine(ctx.Root, "wsl", name);
                Directory.CreateDirectory(disk);
                var files = ctx.Kit.Files("wsl-distro").Select(f => Path.Combine(ctx.Kit.Dir, f)).ToList();
                string tar = files[0];
                if (files.Count > 1)
                {
                    // wsl --import wants one file
                    tar = Path.Combine(ctx.Root, "wsl", "wsl-distro.tar.gz");
                    Log.Info($"joining {files.Count} parts into {tar}");
                    using (var src = ctx.Kit.Open("wsl-distro"))
                    using (var dst = new FileStream(tar, FileMode.Create, FileAccess.Write)) src.CopyTo(dst, 4 << 20);
                }
                Log.Info($"wsl --import {name} (this takes a few minutes)");
                var r = Proc.Run(wsl, $"--import {name} {Proc.Q(disk)} {Proc.Q(tar)}", timeoutMs: 3600000, encoding: Encoding.UTF8, extraEnv: env, echo: true);
                if (tar != files[0]) try { File.Delete(tar); } catch { }
                if (!r.Ok) { ctx.Problems.Add($"wsl --import failed: {r.Tail(5)}"); return; }
                ctx.Rec.Set("wslDistro", name);
            }
            else Log.Info($"{name} is already imported; re-linking it to the caches (pass --replace-wsl to re-import)");

            var link = Proc.Run(wsl, $"-d {name} -u dev -- /usr/local/bin/dev-toolkit-link {Proc.Q(Fs.ToWsl(ctx.Root))}",
                timeoutMs: 600000, encoding: Encoding.UTF8, extraEnv: env, echo: true);
            if (link.Ok) Log.Ok($"{name}: open it with  wsl -d {name}");
            else ctx.Problems.Add($"dev-toolkit-link inside WSL failed: {link.Tail(5)}");
        }
    }
}
