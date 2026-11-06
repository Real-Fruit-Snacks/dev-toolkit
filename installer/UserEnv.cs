using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Runtime.InteropServices;
using Microsoft.Win32;

namespace DevToolkit
{
    /// <summary>Where user environment variables live: HKCU\Environment, or a JSON file in a sandbox.</summary>
    interface IEnvStore
    {
        string Get(string name);
        void Set(string name, string value);
        void Delete(string name);
        Dictionary<string, string> All();
    }

    sealed class RegistryEnvStore : IEnvStore
    {
        const string KeyPath = "Environment";

        public string Get(string name)
        {
            using (var k = Registry.CurrentUser.OpenSubKey(KeyPath))
                return k?.GetValue(name, null, RegistryValueOptions.DoNotExpandEnvironmentNames) as string;
        }

        public void Set(string name, string value)
        {
            using (var k = Registry.CurrentUser.CreateSubKey(KeyPath))
            {
                var kind = value.Contains('%') || name.Equals("Path", StringComparison.OrdinalIgnoreCase)
                    ? RegistryValueKind.ExpandString : RegistryValueKind.String;
                k.SetValue(name, value, kind);
            }
        }

        public void Delete(string name)
        {
            using (var k = Registry.CurrentUser.OpenSubKey(KeyPath, true)) k?.DeleteValue(name, false);
        }

        public Dictionary<string, string> All()
        {
            var d = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            using (var k = Registry.CurrentUser.OpenSubKey(KeyPath))
                if (k != null)
                    foreach (var n in k.GetValueNames())
                        d[n] = k.GetValue(n, null, RegistryValueOptions.DoNotExpandEnvironmentNames) as string ?? "";
            return d;
        }
    }

    sealed class FileEnvStore : IEnvStore
    {
        readonly string path;
        public FileEnvStore(string path) { this.path = path; }

        public Dictionary<string, string> All()
        {
            var d = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            if (File.Exists(path)) foreach (var kv in Json.ReadObject(path)) d[kv.Key] = Convert.ToString(kv.Value);
            return d;
        }

        void Save(Dictionary<string, string> d) => Fs.WriteText(path, Json.Write(d));
        public string Get(string name) => All().TryGetValue(name, out var v) ? v : null;
        public void Set(string name, string value) { var d = All(); d[name] = value; Save(d); }
        public void Delete(string name) { var d = All(); d.Remove(name); Save(d); }
    }

    /// <summary>
    /// Changes the user's environment variables and PATH, records every change for uninstall, and keeps
    /// this process's environment in step so the tools dtk runs next see the new settings.
    /// </summary>
    sealed class UserEnv
    {
        readonly Ctx ctx;
        public readonly IEnvStore Store;

        public UserEnv(Ctx ctx)
        {
            this.ctx = ctx;
            Store = ctx.P.Sandbox ? (IEnvStore)new FileEnvStore(Path.Combine(ctx.P.UserProfile, "user-environment.json")) : new RegistryEnvStore();
        }

        static string Norm(string dir) => Environment.ExpandEnvironmentVariables(dir.Trim().Trim('"')).TrimEnd('\\').ToLowerInvariant();

        public static List<string> SplitPath(string raw) =>
            (raw ?? "").Split(';').Select(s => s.Trim()).Where(s => s.Length > 0).ToList();

        public void SetVar(string name, string value)
        {
            var prev = Store.Get(name);
            ctx.Rec.NoteVar(name, prev);
            if (prev != value) Store.Set(name, value);
            Environment.SetEnvironmentVariable(name, Environment.ExpandEnvironmentVariables(value));
            Log.FileOnly($"   set {name}={value}");
        }

        /// <summary>Puts a folder at the front of the user PATH (ahead of e.g. the WindowsApps python stub).</summary>
        public void AddPath(string dir)
        {
            var raw = Store.Get("Path") ?? "";
            var entries = SplitPath(raw);
            if (!entries.Any(e => Norm(e) == Norm(dir)))
            {
                Store.Set("Path", string.Join(";", new[] { dir }.Concat(entries)));
                ctx.Rec.NotePath(dir);
            }
            var proc = SplitPath(Environment.GetEnvironmentVariable("PATH"));
            if (!proc.Any(e => Norm(e) == Norm(dir)))
                Environment.SetEnvironmentVariable("PATH", string.Join(";", new[] { dir }.Concat(proc)));
        }

        public void RemovePath(string dir)
        {
            var entries = SplitPath(Store.Get("Path"));
            var kept = entries.Where(e => Norm(e) != Norm(dir)).ToList();
            if (kept.Count != entries.Count) Store.Set("Path", string.Join(";", kept));
        }

        /// <summary>Records changes something else (Scoop) made, by comparing before and after.</summary>
        public void RecordDiff(Dictionary<string, string> before)
        {
            var after = Store.All();
            foreach (var kv in after)
            {
                if (kv.Key.Equals("Path", StringComparison.OrdinalIgnoreCase))
                {
                    before.TryGetValue("Path", out var oldPath);
                    var old = new HashSet<string>(SplitPath(oldPath).Select(Norm));
                    foreach (var e in SplitPath(kv.Value)) if (!old.Contains(Norm(e))) ctx.Rec.NotePath(e);
                }
                else if (!before.TryGetValue(kv.Key, out var oldValue) || oldValue != kv.Value)
                {
                    ctx.Rec.NoteVar(kv.Key, before.TryGetValue(kv.Key, out var b) ? b : null);
                }
            }
            // and bring this process up to date with what the other program set
            foreach (var kv in after)
            {
                if (kv.Key.Equals("Path", StringComparison.OrdinalIgnoreCase))
                    foreach (var e in SplitPath(kv.Value).AsEnumerable().Reverse()) AddProcessPath(Environment.ExpandEnvironmentVariables(e));
                else Environment.SetEnvironmentVariable(kv.Key, Environment.ExpandEnvironmentVariables(kv.Value));
            }
        }

        static void AddProcessPath(string dir)
        {
            var proc = SplitPath(Environment.GetEnvironmentVariable("PATH"));
            if (!proc.Any(e => Norm(e) == Norm(dir)))
                Environment.SetEnvironmentVariable("PATH", string.Join(";", new[] { dir }.Concat(proc)));
        }

        /// <summary>Tells Explorer and new terminals that the environment changed, as setx does.</summary>
        public void Broadcast()
        {
            if (ctx.P.Sandbox) return;
            try { Native.SendMessageTimeout((IntPtr)0xffff, 0x1A, UIntPtr.Zero, "Environment", 2, 5000, out _); }
            catch { }
        }

        /// <summary>
        /// The environment a new terminal would get: machine variables, then the user's, with PATH joined
        /// the way Windows joins it. Used by `dtk exec` so tests see the result without signing out.
        /// </summary>
        public static Dictionary<string, string> Fresh(Ctx ctx)
        {
            var d = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            foreach (System.Collections.DictionaryEntry e in Environment.GetEnvironmentVariables()) d[(string)e.Key] = (string)e.Value;
            string machinePath = "";
            using (var k = Registry.LocalMachine.OpenSubKey(@"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"))
                if (k != null)
                    foreach (var n in k.GetValueNames())
                    {
                        var v = Environment.ExpandEnvironmentVariables(k.GetValue(n, "") as string ?? "");
                        if (n.Equals("Path", StringComparison.OrdinalIgnoreCase)) machinePath = v; else d[n] = v;
                    }
            var store = ctx.P.Sandbox ? (IEnvStore)new FileEnvStore(Path.Combine(ctx.P.UserProfile, "user-environment.json")) : new RegistryEnvStore();
            string userPath = "";
            foreach (var kv in store.All())
            {
                var v = Environment.ExpandEnvironmentVariables(kv.Value);
                if (kv.Key.Equals("Path", StringComparison.OrdinalIgnoreCase)) userPath = v; else d[kv.Key] = v;
            }
            d["Path"] = machinePath.TrimEnd(';') + ";" + userPath;
            if (ctx.P.Sandbox)
            {
                // the sandbox has no machine side of its own; keep this process's PATH after the user's
                d["Path"] = userPath.TrimEnd(';') + ";" + Environment.GetEnvironmentVariable("PATH");
                d["USERPROFILE"] = ctx.P.UserProfile; d["HOME"] = ctx.P.UserProfile;
                d["APPDATA"] = ctx.P.AppData; d["LOCALAPPDATA"] = ctx.P.LocalAppData;
            }
            return d;
        }
    }

    /// <summary>installed.json: what dtk installed and every user setting it changed, so uninstall can undo it.</summary>
    sealed class InstallRecord
    {
        public string FilePath;
        public Dictionary<string, object> Data = new Dictionary<string, object>();

        public static InstallRecord Load(string path)
        {
            var r = new InstallRecord { FilePath = path };
            if (File.Exists(path))
            {
                try { r.Data = Json.ReadObject(path); }
                catch (Exception e) { Log.Warn($"{path} is unreadable ({e.Message}); starting a new record"); }
            }
            return r;
        }

        Dictionary<string, object> Map(string key)
        {
            if (!(Data.TryGetValue(key, out var v) && v is Dictionary<string, object> d)) { d = new Dictionary<string, object>(); Data[key] = d; }
            return d;
        }

        List<object> List(string key)
        {
            var l = Data.Arr(key);
            Data[key] = l;
            return l;
        }

        /// <summary>Keeps the value from before the FIRST install, so reinstalling never loses it.</summary>
        public void NoteVar(string name, string previous)
        {
            var vars = Map("vars");
            if (!vars.ContainsKey(name)) vars[name] = previous;
        }

        public void NotePath(string dir)
        {
            var l = List("path");
            if (!l.Cast<string>().Any(x => x.Equals(dir, StringComparison.OrdinalIgnoreCase))) l.Add(dir);
        }

        /// <summary>A config file dtk created (backup null) or changed (backup holds the original).</summary>
        public void NoteFile(string path, string backup)
        {
            var files = Map("files");
            if (!files.ContainsKey(path)) files[path] = backup;
        }

        public void NoteItem(string list, string value)
        {
            var l = List(list);
            if (!l.Cast<string>().Contains(value, StringComparer.OrdinalIgnoreCase)) l.Add(value);
        }

        public void Set(string key, object value) => Data[key] = value;

        public void Save()
        {
            Data["updated"] = DateTime.Now.ToString("o");
            Fs.WriteText(FilePath, Json.Write(Data));
        }

        public Dictionary<string, object> Vars => Map("vars");
        public IEnumerable<string> PathEntries => List("path").Cast<string>();
        public Dictionary<string, object> Files => Map("files");
        public IEnumerable<string> Items(string list) => List(list).Cast<string>();
    }

    static class Native
    {
        [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        public static extern IntPtr SendMessageTimeout(IntPtr hWnd, uint msg, UIntPtr wParam, string lParam, uint flags, uint timeout, out UIntPtr result);

        [DllImport("user32.dll", SetLastError = true)]
        public static extern IntPtr SendMessageTimeout(IntPtr hWnd, uint msg, UIntPtr wParam, IntPtr lParam, uint flags, uint timeout, out UIntPtr result);

        [DllImport("gdi32.dll", CharSet = CharSet.Unicode)]
        public static extern int AddFontResource(string file);

        [DllImport("gdi32.dll", CharSet = CharSet.Unicode)]
        public static extern bool RemoveFontResource(string file);

        public static void FontsChanged()
        {
            try { SendMessageTimeout((IntPtr)0xffff, 0x1D, UIntPtr.Zero, IntPtr.Zero, 2, 2000, out _); } catch { }
        }

        [ComImport, Guid("00021401-0000-0000-C000-000000000046")]
        class ShellLink { }

        [ComImport, InterfaceType(ComInterfaceType.InterfaceIsIUnknown), Guid("000214F9-0000-0000-C000-000000000046")]
        interface IShellLinkW
        {
            void GetPath([Out, MarshalAs(UnmanagedType.LPWStr)] System.Text.StringBuilder file, int max, IntPtr fd, int flags);
            void GetIDList(out IntPtr pidl);
            void SetIDList(IntPtr pidl);
            void GetDescription([Out, MarshalAs(UnmanagedType.LPWStr)] System.Text.StringBuilder name, int max);
            void SetDescription([MarshalAs(UnmanagedType.LPWStr)] string name);
            void GetWorkingDirectory([Out, MarshalAs(UnmanagedType.LPWStr)] System.Text.StringBuilder dir, int max);
            void SetWorkingDirectory([MarshalAs(UnmanagedType.LPWStr)] string dir);
            void GetArguments([Out, MarshalAs(UnmanagedType.LPWStr)] System.Text.StringBuilder args, int max);
            void SetArguments([MarshalAs(UnmanagedType.LPWStr)] string args);
            void GetHotkey(out short hotkey);
            void SetHotkey(short hotkey);
            void GetShowCmd(out int show);
            void SetShowCmd(int show);
            void GetIconLocation([Out, MarshalAs(UnmanagedType.LPWStr)] System.Text.StringBuilder path, int max, out int index);
            void SetIconLocation([MarshalAs(UnmanagedType.LPWStr)] string path, int index);
            void SetRelativePath([MarshalAs(UnmanagedType.LPWStr)] string rel, int reserved);
            void Resolve(IntPtr hwnd, int flags);
            void SetPath([MarshalAs(UnmanagedType.LPWStr)] string file);
        }

        public static void CreateShortcut(string lnk, string target, string args, string workDir)
        {
            Directory.CreateDirectory(Path.GetDirectoryName(lnk));
            var link = (IShellLinkW)new ShellLink();
            link.SetPath(target);
            if (!string.IsNullOrEmpty(args)) link.SetArguments(args);
            link.SetWorkingDirectory(workDir ?? Path.GetDirectoryName(target));
            link.SetIconLocation(target, 0);
            ((System.Runtime.InteropServices.ComTypes.IPersistFile)link).Save(lnk, false);
            Marshal.FinalReleaseComObject(link);
        }
    }
}
