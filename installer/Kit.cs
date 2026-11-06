using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.IO.Compression;
using System.Linq;
using System.Security.Cryptography;

namespace DevToolkit
{
    sealed class Group
    {
        public string Name, Title;
        public bool Required, Optional;
        public List<string> Apps = new List<string>();        // Scoop ids, "bucket/app"
        public List<string> Assets = new List<string>();      // release assets beyond the apps
        public List<string> Extensions = new List<string>();  // VS Code extension ids

        public IEnumerable<string> AppNames => Apps.Select(a => a.Contains('/') ? a.Substring(a.IndexOf('/') + 1) : a);
    }

    /// <summary>A downloaded release: dev-toolkit.json, SHA256SUMS.txt and the asset files, all in one folder.</summary>
    sealed class Kit
    {
        public string Dir;
        public Dictionary<string, object> Manifest;
        public Dictionary<string, string> Sums = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        public List<Group> Groups = new List<Group>();
        Dictionary<string, object> assets;

        public string Version => Manifest.Str("version") ?? "?";

        public static Kit Load(string dir)
        {
            var mf = Path.Combine(dir, "dev-toolkit.json");
            if (!File.Exists(mf))
                throw new UserError($"No dev-toolkit.json in {dir}. Put dtk.exe in the folder with the downloaded release files, or pass --from <folder>.");
            var k = new Kit { Dir = dir, Manifest = Json.ReadObject(mf) };
            k.assets = k.Manifest.Obj("assets") ?? new Dictionary<string, object>();
            var groups = k.Manifest.Obj("groups") ?? new Dictionary<string, object>();
            foreach (var kv in groups)
            {
                var g = (Dictionary<string, object>)kv.Value;
                k.Groups.Add(new Group
                {
                    Name = kv.Key,
                    Title = g.Str("title") ?? kv.Key,
                    Required = g.Bool("required"),
                    Optional = g.Bool("optional"),
                    Apps = g.Strs("apps"),
                    Assets = g.Strs("assets"),
                    Extensions = g.Strs("extensions"),
                });
            }
            var sums = Path.Combine(dir, "SHA256SUMS.txt");
            if (File.Exists(sums))
            {
                foreach (var line in File.ReadAllLines(sums))
                {
                    var t = line.Trim();
                    if (t.Length < 66) continue;
                    var hash = t.Substring(0, 64).ToLowerInvariant();
                    var name = t.Substring(64).Trim().TrimStart('*');
                    k.Sums[name] = hash;
                }
            }
            return k;
        }

        public Group Group(string name) => Groups.FirstOrDefault(g => g.Name.Equals(name, StringComparison.OrdinalIgnoreCase));

        public bool Declares(string asset) => assets.ContainsKey(asset);

        /// <summary>The files that make up an asset: one file, or .part1, .part2, ... in order.</summary>
        public List<string> Files(string asset)
        {
            var a = assets.Obj(asset);
            if (a == null) return new List<string>();
            return a.Strs("files");
        }

        public List<string> MissingFiles(string asset)
        {
            if (!Declares(asset)) return new List<string> { asset + " (not in this release)" };
            return Files(asset).Where(f => !File.Exists(Path.Combine(Dir, f))).ToList();
        }

        public bool Has(string asset) => Declares(asset) && MissingFiles(asset).Count == 0;

        public long Size(string asset) => Files(asset).Select(f => Path.Combine(Dir, f)).Where(File.Exists).Sum(f => new FileInfo(f).Length);

        public Stream Open(string asset)
        {
            var files = Files(asset).Select(f => Path.Combine(Dir, f)).ToList();
            if (files.Count == 0) throw new UserError($"{asset} is not in this release");
            return files.Count == 1
                ? new FileStream(files[0], FileMode.Open, FileAccess.Read, FileShare.Read, 1 << 20)
                : (Stream)new MultiPartStream(files);
        }

        /// <summary>Checks every file of the given assets against SHA256SUMS.txt.</summary>
        public void Verify(IEnumerable<string> assetNames)
        {
            if (Sums.Count == 0) throw new UserError("SHA256SUMS.txt is missing from the release folder, so the files can't be checked. Copy it over, or pass --skip-verify.");
            var files = assetNames.SelectMany(Files).Distinct(StringComparer.OrdinalIgnoreCase).ToList();
            long total = files.Sum(f => new FileInfo(Path.Combine(Dir, f)).Length), done = 0;
            var sw = Stopwatch.StartNew();
            var bad = new List<string>();
            foreach (var f in files)
            {
                var path = Path.Combine(Dir, f);
                if (!Sums.TryGetValue(f, out var want)) { bad.Add($"{f}: not listed in SHA256SUMS.txt"); continue; }
                string got;
                using (var sha = SHA256.Create())
                using (var s = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read, 4 << 20))
                    got = BitConverter.ToString(sha.ComputeHash(s)).Replace("-", "").ToLowerInvariant();
                done += new FileInfo(path).Length;
                if (got != want) { bad.Add($"{f}: SHA-256 doesn't match (the file is damaged or incomplete)"); Log.Error(f); }
                else Log.Ok($"{f}  {Fs.Size(new FileInfo(path).Length)}");
            }
            Log.Info($"checked {files.Count} files, {Fs.Size(total)} in {sw.Elapsed.TotalSeconds:0}s");
            if (bad.Count > 0) throw new UserError("Some files failed the check:\n     " + string.Join("\n     ", bad) + "\n   Copy them over again, then re-run.");
        }

        /// <summary>Unzips an asset into a folder. map turns a zip entry name into a relative output path, or null to skip it.</summary>
        public int Extract(string asset, string dest, Func<string, string> map = null)
        {
            Log.Info($"unpacking {asset} ({Fs.Size(Size(asset))}) -> {dest}");
            using (var s = Open(asset)) return Zip.Extract(s, dest, map);
        }

        public byte[] ReadEntry(string asset, string entry)
        {
            using (var s = Open(asset))
            using (var za = new ZipArchive(s, ZipArchiveMode.Read))
            {
                var e = za.GetEntry(entry);
                if (e == null) return null;
                using (var es = e.Open())
                using (var ms = new MemoryStream())
                {
                    es.CopyTo(ms);
                    return ms.ToArray();
                }
            }
        }
    }

    /// <summary>Reads .part1, .part2, ... as one seekable stream, so split assets never need joining on disk.</summary>
    sealed class MultiPartStream : Stream
    {
        readonly FileStream[] parts;
        readonly long[] starts;
        readonly long length;
        long pos;

        public MultiPartStream(IList<string> files)
        {
            parts = files.Select(f => new FileStream(f, FileMode.Open, FileAccess.Read, FileShare.Read, 1 << 20)).ToArray();
            starts = new long[parts.Length];
            long acc = 0;
            for (int i = 0; i < parts.Length; i++) { starts[i] = acc; acc += parts[i].Length; }
            length = acc;
        }

        public override bool CanRead => true;
        public override bool CanSeek => true;
        public override bool CanWrite => false;
        public override long Length => length;
        public override long Position { get => pos; set => pos = value; }
        public override void Flush() { }
        public override void SetLength(long value) => throw new NotSupportedException();
        public override void Write(byte[] buffer, int offset, int count) => throw new NotSupportedException();

        public override long Seek(long offset, SeekOrigin origin)
        {
            pos = origin == SeekOrigin.Begin ? offset : origin == SeekOrigin.Current ? pos + offset : length + offset;
            return pos;
        }

        public override int Read(byte[] buffer, int offset, int count)
        {
            int total = 0;
            while (count > 0 && pos < length)
            {
                int i = Array.BinarySearch(starts, pos);
                if (i < 0) i = ~i - 1;
                var part = parts[i];
                part.Position = pos - starts[i];
                int n = part.Read(buffer, offset, (int)Math.Min(count, part.Length - part.Position));
                if (n <= 0) break;
                pos += n; offset += n; count -= n; total += n;
            }
            return total;
        }

        protected override void Dispose(bool disposing)
        {
            if (disposing) foreach (var p in parts) p.Dispose();
            base.Dispose(disposing);
        }
    }

    static class Zip
    {
        public static int Extract(Stream s, string dest, Func<string, string> map = null)
        {
            var root = Path.GetFullPath(dest).TrimEnd('\\');
            Directory.CreateDirectory(Fs.Long(root));
            var made = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            int count = 0;
            long bytes = 0;
            var sw = Stopwatch.StartNew();
            var lastReport = sw.Elapsed;
            var buffer = new byte[1 << 20];
            using (var za = new ZipArchive(s, ZipArchiveMode.Read))
            {
                int total = za.Entries.Count;
                foreach (var e in za.Entries)
                {
                    var name = e.FullName.Replace('\\', '/');
                    var rel = map == null ? name : map(name);
                    if (string.IsNullOrEmpty(rel)) continue;
                    var segs = rel.Split(new[] { '/' }, StringSplitOptions.RemoveEmptyEntries);
                    if (segs.Length == 0) continue;
                    if (segs.Any(x => x == ".." || x.Contains(':'))) throw new UserError($"Refusing an unsafe path in the archive: {name}");
                    var target = root + "\\" + string.Join("\\", segs);
                    if (name.EndsWith("/"))
                    {
                        if (made.Add(target)) Directory.CreateDirectory(Fs.Long(target));
                        continue;
                    }
                    var parent = Path.GetDirectoryName(target);
                    if (made.Add(parent)) Directory.CreateDirectory(Fs.Long(parent));
                    var lt = Fs.Long(target);
                    if (File.Exists(lt)) File.SetAttributes(lt, FileAttributes.Normal);
                    using (var src = e.Open())
                    using (var dst = new FileStream(lt, FileMode.Create, FileAccess.Write, FileShare.None, 1 << 16))
                    {
                        int n;
                        while ((n = src.Read(buffer, 0, buffer.Length)) > 0) dst.Write(buffer, 0, n);
                    }
                    try { File.SetLastWriteTimeUtc(lt, e.LastWriteTime.UtcDateTime); } catch { }
                    count++;
                    bytes += e.Length;
                    if ((sw.Elapsed - lastReport).TotalSeconds >= 10)
                    {
                        lastReport = sw.Elapsed;
                        Log.Info($"  ... {count:N0} of {total:N0} files, {Fs.Size(bytes)}");
                    }
                }
            }
            Log.Info($"  {count:N0} files, {Fs.Size(bytes)} in {sw.Elapsed.TotalSeconds:0}s");
            return count;
        }
    }
}
