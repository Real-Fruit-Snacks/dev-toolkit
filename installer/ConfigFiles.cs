using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text;
using System.Text.RegularExpressions;
using System.Xml.Linq;

namespace DevToolkit
{
    /// <summary>
    /// Edits the config files of the user's tools. Only the keys dtk owns change; the rest of a file is kept,
    /// and the original is saved next to it as *.before-dev-toolkit the first time.
    /// </summary>
    static class ConfigFiles
    {
        public static void Backup(Ctx ctx, string path)
        {
            if (ctx.Rec.Files.ContainsKey(path)) return;
            if (File.Exists(path))
            {
                var backup = path + ".before-dev-toolkit";
                if (!File.Exists(backup)) File.Copy(path, backup);
                ctx.Rec.NoteFile(path, backup);
            }
            else ctx.Rec.NoteFile(path, null);
        }

        static List<string> Lines(string path) =>
            File.Exists(path) ? File.ReadAllText(path).Replace("\r\n", "\n").Split('\n').ToList() : new List<string>();

        static void Save(string path, List<string> lines)
        {
            while (lines.Count > 0 && lines[lines.Count - 1].Trim() == "") lines.RemoveAt(lines.Count - 1);
            Fs.WriteText(path, string.Join("\r\n", lines) + "\r\n");
        }

        /// <summary>key=value files (.npmrc).</summary>
        public static void SetKeyValues(Ctx ctx, string path, IDictionary<string, string> values)
        {
            Backup(ctx, path);
            var lines = Lines(path);
            foreach (var kv in values)
            {
                var re = new Regex(@"^\s*" + Regex.Escape(kv.Key) + @"\s*=", RegexOptions.IgnoreCase);
                var line = $"{kv.Key}={kv.Value}";
                var i = lines.FindIndex(l => re.IsMatch(l));
                if (i >= 0) lines[i] = line; else lines.Add(line);
            }
            Save(path, lines);
        }

        /// <summary>INI files (pip.ini): keys within one [section].</summary>
        public static void SetIni(Ctx ctx, string path, string section, IDictionary<string, string> values)
        {
            Backup(ctx, path);
            var lines = Lines(path);
            int start = lines.FindIndex(l => l.Trim().Equals($"[{section}]", StringComparison.OrdinalIgnoreCase));
            if (start < 0) { if (lines.Count > 0) lines.Add(""); lines.Add($"[{section}]"); start = lines.Count - 1; }
            int end = lines.FindIndex(start + 1, l => l.TrimStart().StartsWith("["));
            if (end < 0) end = lines.Count;
            foreach (var kv in values)
            {
                var re = new Regex(@"^\s*" + Regex.Escape(kv.Key) + @"\s*[=:]", RegexOptions.IgnoreCase);
                var line = $"{kv.Key} = {kv.Value}";
                int i = -1;
                for (int j = start + 1; j < end; j++) if (re.IsMatch(lines[j])) { i = j; break; }
                if (i >= 0) lines[i] = line; else { lines.Insert(end, line); end++; }
            }
            Save(path, lines);
        }

        /// <summary>Top-level TOML keys (uv.toml). Values are written as given, so pass TOML literals.</summary>
        public static void SetTomlTopLevel(Ctx ctx, string path, IDictionary<string, string> values)
        {
            Backup(ctx, path);
            var lines = Lines(path);
            int firstTable = lines.FindIndex(l => l.TrimStart().StartsWith("["));
            if (firstTable < 0) firstTable = lines.Count;
            foreach (var kv in values)
            {
                var re = new Regex(@"^\s*" + Regex.Escape(kv.Key) + @"\s*=");
                var line = $"{kv.Key} = {kv.Value}";
                int i = -1;
                for (int j = 0; j < firstTable; j++) if (re.IsMatch(lines[j])) { i = j; break; }
                if (i >= 0) lines[i] = line; else { lines.Insert(firstTable, line); firstTable++; }
            }
            Save(path, lines);
        }

        /// <summary>A file dtk owns outright (Cargo's config in dtk's own CARGO_HOME).</summary>
        public static void WriteOwned(Ctx ctx, string path, string text)
        {
            Backup(ctx, path);
            Fs.WriteText(path, text);
        }

        /// <summary>NuGet.Config: add the local feed, turn nuget.org off, keep everything else.</summary>
        public static void NuGetFeed(Ctx ctx, string path, string feedName, string feedDir)
        {
            Backup(ctx, path);
            XDocument doc;
            try { doc = File.Exists(path) ? XDocument.Load(path) : null; }
            catch (Exception e) { Log.Warn($"{path} isn't valid XML ({e.Message}); leaving it alone"); return; }
            if (doc == null || doc.Root == null) doc = new XDocument(new XDeclaration("1.0", "utf-8", null), new XElement("configuration"));
            var cfg = doc.Root;
            XElement Section(string n) { var s = cfg.Element(n); if (s == null) { s = new XElement(n); cfg.Add(s); } return s; }

            var sources = Section("packageSources");
            foreach (var old in sources.Elements("add").Where(e => (string)e.Attribute("key") == feedName).ToList()) old.Remove();
            sources.Add(new XElement("add", new XAttribute("key", feedName), new XAttribute("value", feedDir)));

            var disabled = Section("disabledPackageSources");
            foreach (var old in disabled.Elements("add").Where(e => (string)e.Attribute("key") == "nuget.org").ToList()) old.Remove();
            disabled.Add(new XElement("add", new XAttribute("key", "nuget.org"), new XAttribute("value", "true")));

            Directory.CreateDirectory(Path.GetDirectoryName(path));
            using (var w = new StreamWriter(path, false, new UTF8Encoding(false))) doc.Save(w);
        }

        /// <summary>VS Code settings.json: our keys merged in. A file with comments is left alone (with a warning).</summary>
        public static void VsCodeSettings(Ctx ctx, string path, IDictionary<string, object> values)
        {
            Backup(ctx, path);
            Dictionary<string, object> cur = new Dictionary<string, object>();
            if (File.Exists(path))
            {
                var text = File.ReadAllText(path);
                if (text.Trim().Length > 0)
                {
                    try { cur = Json.ParseObject(text); }
                    catch { Log.Warn($"{path} has comments or isn't plain JSON; add the offline settings from the README by hand"); return; }
                }
            }
            foreach (var kv in values) cur[kv.Key] = kv.Value;
            Fs.WriteText(path, Json.Write(cur));
        }

        /// <summary>A TOML literal string: no escapes, so Windows paths go in as-is.</summary>
        public static string TomlLit(string s) => "'" + s + "'";
    }
}
