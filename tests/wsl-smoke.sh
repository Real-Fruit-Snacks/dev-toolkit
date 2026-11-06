#!/usr/bin/env bash
# Offline test of the WSL distro on a plain Linux host (GitHub's ubuntu runner).
# Unpacks the distro, lays the caches out where WSL would see the Windows install folder, links them
# with dev-toolkit-link, cuts the network, then builds a Rust, Python, Node and .NET project from the
# caches alone.
#   sudo bash tests/wsl-smoke.sh <folder with the release files>
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run as root"; exit 1; }
KIT="$(cd "${1:?folder with the release files}" && pwd)"
W="${WORK:-/tmp/wsl-smoke}"
ROOTFS="$W/rootfs"
CACHES="$W/dev-toolkit"
WINROOT=/mnt/c/Users/tester/AppData/Local/dev-toolkit

# An asset as one file, joining .part1, .part2, ... if it was split.
whole() {
  local f="$KIT/$1"
  if [ -f "$f" ]; then echo "$f"; return; fi
  ls "$f".part* >/dev/null 2>&1 || { echo "missing $1" >&2; exit 1; }
  # shellcheck disable=SC2046
  cat $(ls "$f".part* | sort -V) > "$W/$1"
  [ -z "${CONSUME_KIT:-}" ] || rm -f "$f".part*
  echo "$W/$1"
}

# Unzips an asset, then drops the joined copy (and, with CONSUME_KIT=1, the download) to save disk.
unpack() {
  local z; z="$(whole "$1")"
  unzip -q "$z" -d "$2"
  if [ "$z" = "$W/$1" ] || [ -n "${CONSUME_KIT:-}" ]; then rm -f "$z"; fi
  df -h "$W" | tail -1
}

cleanup() {
  for m in "$ROOTFS$WINROOT" "$ROOTFS/dev" "$ROOTFS/sys" "$ROOTFS/proc"; do umount -R "$m" 2>/dev/null || true; done
}
trap cleanup EXIT
rm -rf "$W"; mkdir -p "$ROOTFS" "$CACHES/wheels"

echo "== distro"
distro="$(whole wsl-distro.tar.gz)"
tar -xzf "$distro" -C "$ROOTFS"
if [ "$distro" = "$W/wsl-distro.tar.gz" ] || [ -n "${CONSUME_KIT:-}" ]; then rm -f "$distro"; fi
echo "== caches, laid out as dtk.exe lays them out"
unpack crates.zip "$CACHES/crates"
unpack wheels-linux.zip "$CACHES/wheels/linux"
unpack npm-cache.zip "$CACHES/npm-cache"
unpack nuget.zip "$CACHES/nuget"
# files under /mnt/c belong to the WSL user, so let dev write where the tools write
chmod -R a+rwX "$CACHES"

mkdir -p "$ROOTFS$WINROOT"
mount --bind "$CACHES" "$ROOTFS$WINROOT"
mount -t proc proc "$ROOTFS/proc"
mount --rbind /sys "$ROOTFS/sys"; mount --make-rslave "$ROOTFS/sys"
mount --rbind /dev "$ROOTFS/dev"; mount --make-rslave "$ROOTFS/dev"

uid=$(chroot "$ROOTFS" id -u dev)
gid=$(chroot "$ROOTFS" id -g dev)
# Runs a script as dev in a login shell, so /etc/profile.d and ~/.profile apply as they would in WSL.
as_dev() {
  chroot --userspec="$uid:$gid" "$ROOTFS" /usr/bin/env -i HOME=/home/dev USER=dev LOGNAME=dev TERM=dumb     PATH=/home/dev/.cargo/bin:/home/dev/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin     bash -l "$@"
}

echo "== wsl.conf"
cat "$ROOTFS/etc/wsl.conf"
grep -Eq '^ *default *= *dev' "$ROOTFS/etc/wsl.conf"

echo "== dev-toolkit-link"
as_dev -c "dev-toolkit-link $WINROOT"

cat > "$ROOTFS/tmp/offline-test.sh" <<'TEST'
set -euo pipefail
if curl -sS -m 5 https://example.com >/dev/null 2>&1; then echo "the network is up; this test must run without it"; exit 1; fi
cd /tmp && rm -rf t && mkdir t && cd t

echo "-- rust"
rustc --version
cargo new -q hello && cd hello
cargo add -q serde --features derive
cargo add -q serde_json anyhow
cat > src/main.rs <<'RS'
use serde::{Deserialize, Serialize};
#[derive(Serialize, Deserialize, Debug)]
struct Point { x: i32, y: i32 }
fn main() -> anyhow::Result<()> {
    let p: Point = serde_json::from_str(r#"{"x":1,"y":2}"#)?;
    println!("rust ok {:?}", p);
    Ok(())
}
RS
cargo run -q
cd ..

echo "-- python"
python3 --version
python3 -m venv v
v/bin/pip install -q requests numpy pandas
v/bin/python -c 'import pandas, requests; print("python ok", pandas.__version__)'
uv venv -q u
uv pip install -q --python u/bin/python fastapi httpx
u/bin/python -c 'import fastapi; print("uv ok", fastapi.__version__)'

echo "-- node"
node --version
mkdir web && cd web && npm init -y >/dev/null
npm install express typescript
node -e 'require("express"); console.log("node ok")'
npx tsc --version
cd ..

echo "-- dotnet"
dotnet --version
dotnet new console -o con >/dev/null
dotnet add con package Newtonsoft.Json
dotnet run --project con
echo "all passed"
TEST
chmod 0755 "$ROOTFS/tmp/offline-test.sh"

echo "== offline builds (no network namespace)"
export -f as_dev
export ROOTFS uid gid
unshare -n bash -c 'as_dev /tmp/offline-test.sh'
