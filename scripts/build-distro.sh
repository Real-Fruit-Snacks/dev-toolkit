#!/usr/bin/env bash
# Builds wsl-distro.tar.gz: Ubuntu 24.04 for WSL with dev-toolkit's toolchains
# baked in. Runs as root on a plain Ubuntu host with network access (GitHub
# Actions ubuntu-latest, or a throwaway WSL distro).
#
#   sudo scripts/build-distro.sh [options]      (--help lists them)
#
# 1. Downloads the newest Ubuntu 24.04.x WSL image from releases.ubuntu.com and
#    checks it against SHA256SUMS, whose signature is checked with the host's
#    Ubuntu archive keyring.
# 2. Unpacks it into a work folder and runs wsl/provision.sh in a chroot.
# 3. Cleans caches, logs and build-time files out of the tree and scans it for
#    anything that looks like a credential.
# 4. Packs it with numeric owners (no resolv.conf; WSL writes its own) and
#    writes <name>.facts.json next to the tarball.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$REPO/out/wsl-distro.tar.gz"
WORK=/var/tmp/dtk-distro
NODE_MAJOR=26
DOTNET_CHANNEL=10.0
PYTHON_VERSION=3.14
UV_VERSION=latest
IMAGE_INDEX=https://releases.ubuntu.com/noble
IMAGE_URL=""
IMAGE_FILE=""
IMAGE_SHA256=""
KEYRING=/usr/share/keyrings/ubuntu-archive-keyring.gpg
KEEP_WORK=0
DEV_USER=dev

usage() {
  cat <<EOF
Usage: sudo scripts/build-distro.sh [options]

  --out FILE            Output tarball (default: $OUT)
  --node-major N        Node.js LTS major line (default: $NODE_MAJOR)
  --dotnet-channel X    .NET SDK channel (default: $DOTNET_CHANNEL)
  --python X.Y          Python version installed through uv (default: $PYTHON_VERSION)
  --uv-version X.Y.Z    uv release (default: latest)
  --image-url URL       Ubuntu WSL image to start from (default: the newest
                        ubuntu-24.04.x-wsl-amd64.wsl in $IMAGE_INDEX/SHA256SUMS)
  --image-file FILE     Use an image that is already downloaded (still checked)
  --image-sha256 HEX    Expected SHA-256 of the image, instead of the signed SHA256SUMS
  --keyring FILE        Keyring for SHA256SUMS.gpg (default: $KEYRING)
  --work DIR            Scratch folder on a Linux filesystem (default: $WORK)
  --keep-work           Leave the unpacked tree in the work folder afterwards
  -h, --help            Show this help
EOF
}

die() { printf 'build-distro: %s\n' "$*" >&2; exit 1; }
log() { printf '\n== %s\n' "$*"; }
need_arg() { [ "$#" -ge 2 ] && [ -n "$2" ] || die "$1 needs a value"; }

while [ "$#" -gt 0 ]; do
  case "$1" in
    --out)            need_arg "$@"; OUT="$2"; shift 2 ;;
    --node-major)     need_arg "$@"; NODE_MAJOR="$2"; shift 2 ;;
    --dotnet-channel) need_arg "$@"; DOTNET_CHANNEL="$2"; shift 2 ;;
    --python)         need_arg "$@"; PYTHON_VERSION="$2"; shift 2 ;;
    --uv-version)     need_arg "$@"; UV_VERSION="$2"; shift 2 ;;
    --image-url)      need_arg "$@"; IMAGE_URL="$2"; shift 2 ;;
    --image-file)     need_arg "$@"; IMAGE_FILE="$2"; shift 2 ;;
    --image-sha256)   need_arg "$@"; IMAGE_SHA256="${2,,}"; shift 2 ;;
    --keyring)        need_arg "$@"; KEYRING="$2"; shift 2 ;;
    --work)           need_arg "$@"; WORK="$2"; shift 2 ;;
    --keep-work)      KEEP_WORK=1; shift ;;
    -h|--help)        usage; exit 0 ;;
    *)                usage >&2; die "unknown option: $1" ;;
  esac
done

[ "$(id -u)" = 0 ] || die "run as root (sudo)"
[[ "$NODE_MAJOR" =~ ^[0-9]+$ ]] || die "--node-major must be a number"
[[ "$WORK" = /* ]] || die "--work must be an absolute path"
for tool in curl tar gzip sha256sum chroot mount umount findmnt mountpoint; do
  command -v "$tool" >/dev/null || die "missing tool on the host: $tool"
done
[ -f "$REPO/wsl/provision.sh" ] && [ -f "$REPO/wsl/dev-toolkit-link" ] || die "wsl/provision.sh or wsl/dev-toolkit-link not found under $REPO"

OUT="$(realpath -m "$OUT")"
case "$OUT" in
  *.tar.gz) FACTS="${OUT%.tar.gz}.facts.json" ;;
  *)        FACTS="$OUT.facts.json" ;;
esac
CH="$WORK/rootfs"
DL="$WORK/download"
START=$(date +%s)

# ---------------------------------------------------------------- mounts --
# Everything mounted under $CH is listed here, so cleanup can undo it and the
# tree is never deleted while host folders are still bound inside it.
mounts_under_ch() { findmnt -rn -o TARGET | awk -v p="$CH/" 'index($0, p) == 1' | sort -r; }

unmount_all() {
  local m
  for m in $(mounts_under_ch); do
    umount -R "$m" 2>/dev/null || umount -R -l "$m" 2>/dev/null || true
  done
  [ -z "$(mounts_under_ch)" ]
}

remove_tree() {
  [ -d "$CH" ] || return 0
  unmount_all || die "could not unmount: $(mounts_under_ch | tr '\n' ' ')"
  rm -rf --one-file-system "$CH"
}

on_exit() {
  local rc=$?
  unmount_all || printf 'build-distro: still mounted: %s\n' "$(mounts_under_ch | tr '\n' ' ')" >&2
  if [ "$rc" != 0 ] && [ -d "$CH" ]; then
    printf '\nbuild-distro: failed (exit %s); the unpacked tree is left in %s\n' "$rc" "$CH" >&2
  fi
}
trap on_exit EXIT

# ------------------------------------------------------- the Ubuntu image --
fetch() { curl --proto '=https' --tlsv1.2 -fsSL --retry 3 --retry-delay 3 -o "$2" "$1"; }

log "Ubuntu WSL image"
mkdir -p "$DL"
if [ -z "$IMAGE_SHA256" ]; then
  # SHA256SUMS sits next to the image; its detached signature is made with the
  # Ubuntu CD image key, which ships in ubuntu-keyring's archive keyring.
  if [ -n "$IMAGE_URL" ]; then SUMS_BASE="${IMAGE_URL%/*}"; else SUMS_BASE="$IMAGE_INDEX"; fi
  command -v gpgv >/dev/null || die "gpgv is needed to check SHA256SUMS (or pass --image-sha256)"
  [ -f "$KEYRING" ] || die "keyring $KEYRING not found (install ubuntu-keyring, or pass --keyring / --image-sha256)"
  fetch "$SUMS_BASE/SHA256SUMS" "$DL/SHA256SUMS"
  fetch "$SUMS_BASE/SHA256SUMS.gpg" "$DL/SHA256SUMS.gpg"
  gpgv --keyring "$KEYRING" "$DL/SHA256SUMS.gpg" "$DL/SHA256SUMS" 2>"$DL/gpgv.log" \
    || { cat "$DL/gpgv.log" >&2; die "SHA256SUMS signature check failed"; }
  echo "SHA256SUMS signature is good ($(grep -o 'using .*' "$DL/gpgv.log" | head -1))"
fi

if [ -n "$IMAGE_FILE" ]; then
  [ -f "$IMAGE_FILE" ] || die "image file not found: $IMAGE_FILE"
  IMAGE_NAME="$(basename "$IMAGE_FILE")"
  IMAGE_SOURCE="$IMAGE_FILE"
elif [ -n "$IMAGE_URL" ]; then
  IMAGE_NAME="${IMAGE_URL##*/}"
  IMAGE_SOURCE="$IMAGE_URL"
else
  # Newest point release, e.g. ubuntu-24.04.5-wsl-amd64.wsl
  IMAGE_NAME=$(awk '{ sub(/^\*/, "", $2); print $2 }' "$DL/SHA256SUMS" \
    | grep -E '^ubuntu-24\.04(\.[0-9]+)?-wsl-amd64\.wsl$' | sort -V | tail -1 || true)
  [ -n "$IMAGE_NAME" ] || die "no ubuntu-24.04*-wsl-amd64.wsl listed in $IMAGE_INDEX/SHA256SUMS"
  IMAGE_SOURCE="$IMAGE_INDEX/$IMAGE_NAME"
fi

if [ -z "$IMAGE_SHA256" ]; then
  IMAGE_SHA256=$(awk -v n="$IMAGE_NAME" '{ f = $2; sub(/^\*/, "", f); if (f == n) print $1 }' "$DL/SHA256SUMS")
  [ -n "$IMAGE_SHA256" ] || die "$IMAGE_NAME is not listed in SHA256SUMS"
fi
[[ "$IMAGE_SHA256" =~ ^[0-9a-f]{64}$ ]] || die "not a SHA-256: $IMAGE_SHA256"

if [ -n "$IMAGE_FILE" ]; then
  IMAGE="$IMAGE_FILE"
else
  IMAGE="$DL/$IMAGE_NAME"
  # a copy from an earlier run is reused when it still matches
  if ! { [ -f "$IMAGE" ] && echo "$IMAGE_SHA256  $IMAGE" | sha256sum -c --status -; }; then
    echo "downloading $IMAGE_SOURCE"
    fetch "$IMAGE_SOURCE" "$IMAGE.part"
    mv "$IMAGE.part" "$IMAGE"
  fi
fi
echo "$IMAGE_SHA256  $IMAGE" | sha256sum -c --quiet - || die "SHA-256 mismatch for $IMAGE"
echo "$IMAGE_NAME: SHA-256 $IMAGE_SHA256 matches"

# ------------------------------------------------------------- unpacking --
log "Unpacking into $CH"
remove_tree
mkdir -p "$CH"
# The work folder must keep Linux owners and modes (not /mnt/c or another
# Windows-backed mount).
probe="$WORK/.owner-probe"
rm -f "$probe"; touch "$probe"
chown 12345:12345 "$probe" 2>/dev/null && [ "$(stat -c %u "$probe")" = 12345 ] \
  || { rm -f "$probe"; die "$WORK does not keep file owners; use --work on a Linux filesystem"; }
rm -f "$probe"
tar -xzf "$IMAGE" -C "$CH" --numeric-owner --xattrs --xattrs-include='*'
[ -x "$CH/bin/bash" ] && [ -f "$CH/etc/os-release" ] || die "$IMAGE_NAME does not look like a root filesystem"

# Remember what the image ships for files the build touches, to put them back.
if [ -e "$CH/etc/machine-id" ]; then MACHINE_ID_STATE="$(stat -c %s "$CH/etc/machine-id")"; else MACHINE_ID_STATE=absent; fi

log "Preparing the chroot"
mount -t proc proc "$CH/proc"
mount --rbind /sys "$CH/sys";  mount --make-rslave "$CH/sys"
mount --rbind /dev "$CH/dev";  mount --make-rslave "$CH/dev"
# A fresh /run, so package scripts don't see (and talk to) the host's systemd.
mount -t tmpfs -o mode=0755,nosuid,nodev tmpfs "$CH/run"
mkdir -p "$CH/run/lock"
rm -f "$CH/etc/resolv.conf"
cat /etc/resolv.conf > "$CH/etc/resolv.conf"
# Keep package scripts from starting services in the chroot.
printf '#!/bin/sh\nexit 101\n' > "$CH/usr/sbin/policy-rc.d"
chmod 0755 "$CH/usr/sbin/policy-rc.d"

SRC=/tmp/dtk-provision
mkdir -p "$CH$SRC"
# strip CRs in case the checkout came through Windows line endings
for f in provision.sh dev-toolkit-link; do
  sed 's/\r$//' "$REPO/wsl/$f" > "$CH$SRC/$f"
  chmod 0755 "$CH$SRC/$f"
done

CHROOT_PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
in_chroot() {
  chroot "$CH" /usr/bin/env -i HOME=/root LANG=C.UTF-8 PATH="$CHROOT_PATH" TERM=dumb "$@"
}

# ----------------------------------------------------------- provisioning --
log "Provisioning (wsl/provision.sh in the chroot)"
in_chroot DEBIAN_FRONTEND=noninteractive /bin/bash "$SRC/provision.sh" \
  --user "$DEV_USER" --node-major "$NODE_MAJOR" --dotnet-channel "$DOTNET_CHANNEL" \
  --python "$PYTHON_VERSION" --uv-version "$UV_VERSION" --link-script "$SRC/dev-toolkit-link"

# ----------------------------------------------------------------- facts --
log "Versions"
# Ask the tools themselves, as the dev user with the same environment a login
# shell gets from /etc/profile.d/dev-toolkit.sh.
as_dev() {
  in_chroot /usr/bin/setpriv --reuid="$DEV_USER" --regid="$DEV_USER" --init-groups \
    /bin/bash -c "cd ~$DEV_USER && export HOME=~$DEV_USER && . /etc/profile.d/dev-toolkit.sh && $1"
}
field() { awk -v n="$1" '{ print $n; exit }'; }
V_UBUNTU=$(. "$CH/etc/os-release" && echo "${VERSION%% *}")
V_RUST=$(as_dev 'rustc --version' | field 2)
V_PYTHON=$(as_dev 'python --version' | field 2)
V_NODE=$(as_dev 'node --version' | sed 's/^v//')
V_NPM=$(as_dev 'npm --version')
V_DOTNET=$(as_dev 'dotnet --version')
V_DOTNET_RUNTIME=$(as_dev 'dotnet --list-runtimes' | awk '$1 == "Microsoft.NETCore.App" { v = $2 } END { print v }')
V_UV=$(as_dev 'uv --version' | field 2)
V_GIT=$(as_dev 'git --version' | field 3)
V_GCC=$(as_dev 'gcc -dumpfullversion')
V_CLANG=$(as_dev 'clang --version' | sed -nE '1s/.*version ([0-9.]+).*/\1/p')
V_CMAKE=$(as_dev 'cmake --version' | field 3)
for v in V_UBUNTU V_RUST V_PYTHON V_NODE V_NPM V_DOTNET V_DOTNET_RUNTIME V_UV V_GIT V_GCC V_CLANG V_CMAKE; do
  [[ "${!v}" =~ ^[0-9][0-9A-Za-z.+~-]*$ ]] || die "could not read a version for $v (got '${!v}')"
  printf '  %-17s %s\n' "${v#V_}" "${!v}"
done

# ----------------------------------------------------------------- clean --
log "Cleaning"
unmount_all || die "could not unmount: $(mounts_under_ch | tr '\n' ' ')"
rm -f "$CH/usr/sbin/policy-rc.d"
rm -f "$CH/etc/resolv.conf"
case "$MACHINE_ID_STATE" in
  absent) rm -f "$CH/etc/machine-id" ;;
  0)      : > "$CH/etc/machine-id" ;;
  *)      : ;;   # the image ships a real one; leave it
esac
DEV_HOME="$CH/home/$DEV_USER"
# Drop the contents, keep the folders themselves.
for d in tmp var/tmp var/cache/apt/archives var/lib/apt/lists; do
  if [ -d "$CH/$d" ]; then
    find "$CH/$d" -mindepth 1 -maxdepth 1 ! -name partial ! -name lock -exec rm -rf {} +
  fi
done
rm -rf "$CH"/var/cache/apt/*.bin "$CH"/var/cache/apt/archives/partial/* "$CH"/var/lib/apt/lists/partial/*
rm -f "$CH"/var/cache/debconf/*-old "$CH"/var/lib/dpkg/*-old
rm -f "$CH/var/lib/systemd/random-seed"
find "$CH/var/log" -type f \( -name '*.gz' -o -name '*.[0-9]' -o -name '*.old' \) -delete
find "$CH/var/log" -type f -exec truncate -s 0 {} +
rm -rf "$CH/root/.cache" "$CH/root/.npm" "$CH/root/.dotnet" "$CH/root/.wget-hsts" "$CH/root/.lesshst" \
  "$DEV_HOME/.cache" "$DEV_HOME/.npm" "$DEV_HOME/.dotnet" "$DEV_HOME/.nuget" \
  "$DEV_HOME/.local/share/NuGet" "$DEV_HOME/.wget-hsts" "$DEV_HOME/.lesshst" \
  "$DEV_HOME/.sudo_as_admin_successful" "$DEV_HOME/.cargo/registry" "$DEV_HOME/.cargo/git" \
  "$DEV_HOME/.rustup/downloads" "$DEV_HOME/.rustup/tmp"
find "$CH/root" "$CH/home" -maxdepth 2 -type f -name '.*_history' -delete

# ---------------------------------------------------------- secret scan --
log "Credential scan"
hits="$WORK/scan-hits.txt"
: > "$hits"
# Files that should never ship in an image, wherever they are.
find "$CH" -xdev \( -path "$CH/proc" -o -path "$CH/sys" -o -path "$CH/dev" \) -prune -o -type f \( \
    -name '.git-credentials' -o -name '.netrc' -o -name '_netrc' -o -name '.pypirc' \
    -o -name 'id_rsa' -o -name 'id_dsa' -o -name 'id_ecdsa' -o -name 'id_ed25519' \
    -o -name 'id_ecdsa_sk' -o -name 'id_ed25519_sk' -o -name '.credentials.json' \
    -o -name '*_history' -o -name 'ssh_host_*_key' \
    -o -path '*/.config/gh/hosts.yml' -o -path '*/.docker/config.json' \
    -o -path '*/.aws/credentials' -o -path '*/.kube/config' -o -path '*/.azure/*' \
  \) -print >> "$hits"
# Anything in a home folder's .ssh, and per-user tool configs that can hold
# tokens (dev-toolkit-link writes these later, on the target machine).
find "$CH/root" "$CH/home" -xdev \( -path '*/.ssh/*' -o -name '.npmrc' -o -name '.yarnrc*' \
  -o -name 'NuGet.Config' -o -name 'credentials.toml' -o -name 'credentials' \) -type f -print >> "$hits"
# Private keys outside the system's public certificate store.
find "$CH/etc/ssl/private" -type f -size +0 -print >> "$hits" 2>/dev/null || true
# Token-shaped strings in config and home folders (toolchain trees skipped:
# their test suites carry sample keys on purpose).
grep -rIlE --exclude-dir=.rustup --exclude-dir=uv \
  'gh[pousr]_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{40,}|-----BEGIN ([A-Z]+ )?PRIVATE KEY-----|AKIA[0-9A-Z]{16}|_authToken=|xox[abprs]-[A-Za-z0-9-]{10,}' \
  "$CH/etc" "$CH/root" "$CH/home" >> "$hits" 2>/dev/null || true
if [ -s "$hits" ]; then
  sed "s|^$CH||" "$hits" >&2
  die "credential-looking files in the tree (listed above); nothing was packed"
fi
echo "nothing found"

# ------------------------------------------------------------------ pack --
log "Packing $OUT"
UNPACKED_BYTES=$(du -sxb "$CH" | field 1)
mkdir -p "$(dirname "$OUT")"
if command -v pigz >/dev/null; then GZIP_CMD=(pigz -9 -p "$(nproc)"); else GZIP_CMD=(gzip -9); fi
rm -f "$OUT" "$OUT.part"
tar -C "$CH" --numeric-owner --xattrs --xattrs-include='*' --sparse -cf - . | "${GZIP_CMD[@]}" > "$OUT.part"
mv "$OUT.part" "$OUT"
SIZE_BYTES=$(stat -c %s "$OUT")
OUT_SHA256=$(sha256sum "$OUT" | field 1)
BUILD_SECONDS=$(( $(date +%s) - START ))

json_str() { local s=${1//\\/\\\\}; s=${s//\"/\\\"}; printf '"%s"' "$s"; }
{
  printf '{\n'
  printf '  "asset": %s,\n' "$(json_str "$(basename "$OUT")")"
  printf '  "ubuntu": %s,\n' "$(json_str "$V_UBUNTU")"
  printf '  "image": { "name": %s, "sha256": %s },\n' "$(json_str "$IMAGE_NAME")" "$(json_str "$IMAGE_SHA256")"
  printf '  "user": %s,\n' "$(json_str "$DEV_USER")"
  printf '  "rust": %s,\n' "$(json_str "$V_RUST")"
  printf '  "python": %s,\n' "$(json_str "$V_PYTHON")"
  printf '  "uv": %s,\n' "$(json_str "$V_UV")"
  printf '  "node": %s,\n' "$(json_str "$V_NODE")"
  printf '  "npm": %s,\n' "$(json_str "$V_NPM")"
  printf '  "dotnetSdk": %s,\n' "$(json_str "$V_DOTNET")"
  printf '  "dotnetRuntime": %s,\n' "$(json_str "$V_DOTNET_RUNTIME")"
  printf '  "tools": { "git": %s, "gcc": %s, "clang": %s, "cmake": %s },\n' \
    "$(json_str "$V_GIT")" "$(json_str "$V_GCC")" "$(json_str "$V_CLANG")" "$(json_str "$V_CMAKE")"
  printf '  "sizeBytes": %s,\n' "$SIZE_BYTES"
  printf '  "unpackedBytes": %s,\n' "$UNPACKED_BYTES"
  printf '  "sha256": %s,\n' "$(json_str "$OUT_SHA256")"
  printf '  "buildSeconds": %s,\n' "$BUILD_SECONDS"
  printf '  "builtAt": %s\n' "$(json_str "$(date -u +%Y-%m-%dT%H:%M:%SZ)")"
  printf '}\n'
} > "$FACTS"

if [ "$KEEP_WORK" = 1 ]; then
  echo "unpacked tree kept in $CH"
else
  remove_tree
fi
log "Done in ${BUILD_SECONDS}s"
printf '%s  %s MiB (unpacked %s MiB)\n%s\n' "$OUT" "$(( SIZE_BYTES / 1048576 ))" "$(( UNPACKED_BYTES / 1048576 ))" "$FACTS"
