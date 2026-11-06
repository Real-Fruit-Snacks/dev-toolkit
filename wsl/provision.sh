#!/usr/bin/env bash
# Provisions the dev-toolkit WSL distro. Runs as root inside the chroot that
# scripts/build-distro.sh prepares, with network access. Running it again
# brings everything up to date.
#
#   bash provision.sh [--user dev] [--node-major 26] [--dotnet-channel 10.0]
#                     [--python 3.14] [--uv-version latest] [--link-script PATH]
#
# Every download is checked before use: Node.js against SHASUMS256.txt, uv
# and rustup-init against their published .sha256 files, and the .NET SDK
# archive against the sha512 in Microsoft's release metadata. rustup and uv
# check the toolchains and Python builds they fetch themselves.
set -euo pipefail

DEV_USER=dev
NODE_MAJOR=26
DOTNET_CHANNEL=10.0
PYTHON_VERSION=3.14
UV_VERSION=latest
LINK_SCRIPT="$(dirname "$(readlink -f "$0")")/dev-toolkit-link"

APT_PACKAGES=(
  # compilers, build and debug tools
  build-essential pkg-config cmake ninja-build clang lld gdb
  # the headers native builds (Rust -sys crates, Python extensions) ask for most
  libssl-dev zlib1g-dev
  # source control, downloads and archives
  git openssh-client curl ca-certificates unzip zip xz-utils zstd bzip2
  # database clients
  sqlite3 postgresql-client
  # shell tools
  jq ripgrep fd-find less file bash-completion sudo
  # ICU for .NET; the system Python 3 runs dev-toolkit-link
  libicu74 tzdata python3
)
# Agents in the stock image that only work online.
APT_PURGE=(snapd landscape-client landscape-common wsl-pro-service)

die() { printf 'provision: %s\n' "$*" >&2; exit 1; }
log() { printf '\n== %s\n' "$*"; }
need_arg() { [ "$#" -ge 2 ] && [ -n "$2" ] || die "$1 needs a value"; }

while [ "$#" -gt 0 ]; do
  case "$1" in
    --user)           need_arg "$@"; DEV_USER="$2"; shift 2 ;;
    --node-major)     need_arg "$@"; NODE_MAJOR="$2"; shift 2 ;;
    --dotnet-channel) need_arg "$@"; DOTNET_CHANNEL="$2"; shift 2 ;;
    --python)         need_arg "$@"; PYTHON_VERSION="$2"; shift 2 ;;
    --uv-version)     need_arg "$@"; UV_VERSION="$2"; shift 2 ;;
    --link-script)    need_arg "$@"; LINK_SCRIPT="$2"; shift 2 ;;
    -h|--help)        sed -n '2,12p' "$0"; exit 0 ;;
    *)                die "unknown option: $1" ;;
  esac
done

[ "$(id -u)" = 0 ] || die "run as root"
[ -f "$LINK_SCRIPT" ] || die "dev-toolkit-link not found at $LINK_SCRIPT"

DL=$(mktemp -d /tmp/dtk-dl.XXXXXX)
chmod 0755 "$DL"   # the dev user runs rustup-init from here
trap 'rm -rf "$DL"' EXIT

fetch() { curl --proto '=https' --tlsv1.2 -fsSL --retry 3 --retry-delay 3 -o "$2" "$1"; }
check_sha256() {
  [[ "$2" =~ ^[0-9a-f]{64}$ ]] || die "no SHA-256 published for $(basename "$1")"
  echo "$2  $1" | sha256sum -c --quiet - || die "SHA-256 mismatch for $(basename "$1")"
}

# ------------------------------------------------------------------ apt --
log "apt packages"
export DEBIAN_FRONTEND=noninteractive
APT_OPTS=(-y -q -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold)
purge=()
for p in "${APT_PURGE[@]}"; do
  if dpkg-query -W -f='${Status}' "$p" 2>/dev/null | grep -q 'install ok installed'; then purge+=("$p"); fi
done
if [ "${#purge[@]}" -gt 0 ]; then apt-get "${APT_OPTS[@]}" purge "${purge[@]}"; fi
apt-get update -q
apt-get "${APT_OPTS[@]}" dist-upgrade
apt-get "${APT_OPTS[@]}" install --no-install-recommends "${APT_PACKAGES[@]}"
# What only the purged agents needed (squashfs-tools, fuse3, twisted, ...);
# bc came in the same way but build scripts still call it.
if dpkg-query -W bc >/dev/null 2>&1; then apt-mark manual bc >/dev/null; fi
apt-get "${APT_OPTS[@]}" autoremove --purge
# Ubuntu names fd's binary fdfind
ln -sfn /usr/bin/fdfind /usr/local/bin/fd

# --------------------------------------------------------------- system --
log "System settings"
# The image's first-run setup asks for a user name and installs a font into
# the Windows profile; the user already exists here, so take its command out.
if [ -f /etc/wsl-distribution.conf ]; then
  awk '/^[[:space:]]*\[/ { s = $0 } !(s ~ /^[[:space:]]*\[oobe\]/ && $0 ~ /^[[:space:]]*command[[:space:]]*=/)' \
    /etc/wsl-distribution.conf > /etc/wsl-distribution.conf.new
  mv /etc/wsl-distribution.conf.new /etc/wsl-distribution.conf
fi
# cloud-init only matters for provisioning from Windows-side user data, which
# this distro doesn't use; it would still run on every boot.
if [ -d /etc/cloud ]; then touch /etc/cloud/cloud-init.disabled; fi
# No fetching of news for the login message.
if [ -f /etc/default/motd-news ]; then sed -i 's/^ENABLED=1/ENABLED=0/' /etc/default/motd-news; fi

# ----------------------------------------------------------------- user --
log "User $DEV_USER"
if ! id -u "$DEV_USER" >/dev/null 2>&1; then
  # uid 1000 is what WSL gives the first user, and what /mnt/c files map to
  if getent passwd 1000 >/dev/null; then uid_opt=(); else uid_opt=(--uid 1000); fi
  useradd --create-home --shell /bin/bash --user-group "${uid_opt[@]}" "$DEV_USER"
fi
# the groups Ubuntu's own WSL setup gives its first user
usermod -aG adm,cdrom,sudo,dip,plugdev "$DEV_USER"
printf '%s ALL=(ALL:ALL) NOPASSWD: ALL\n' "$DEV_USER" > /etc/sudoers.d/dev-toolkit.new
chmod 0440 /etc/sudoers.d/dev-toolkit.new
visudo -cqf /etc/sudoers.d/dev-toolkit.new || die "sudoers entry did not validate"
mv /etc/sudoers.d/dev-toolkit.new /etc/sudoers.d/dev-toolkit
DEV_HOME=$(getent passwd "$DEV_USER" | cut -d: -f6)

as_dev() {
  (cd "$DEV_HOME" && setpriv --reuid="$DEV_USER" --regid="$DEV_USER" --init-groups \
    env -i HOME="$DEV_HOME" USER="$DEV_USER" LOGNAME="$DEV_USER" SHELL=/bin/bash LANG=C.UTF-8 TERM=dumb \
      PATH="$DEV_HOME/.local/bin:$DEV_HOME/.cargo/bin:/usr/local/bin:/usr/bin:/bin" "$@")
}

cat > /etc/wsl.conf <<EOF
# Written by dev-toolkit's wsl/provision.sh.
[boot]
systemd=true

[user]
default=$DEV_USER

[interop]
# Keep the Windows PATH out, so Windows toolchains (cargo.exe, node.exe,
# python.exe, dotnet.exe) can't shadow the Linux ones. Windows programs still
# run when called by full path, e.g. /mnt/c/Windows/explorer.exe.
appendWindowsPath=false
EOF

# -------------------------------------------------------------- Node.js --
log "Node.js $NODE_MAJOR"
fetch https://nodejs.org/dist/index.json "$DL/node-index.json"
# The newest release of that major; LTS releases first, in case the line has
# not gone LTS yet.
node_line=$(jq -r --arg p "v$NODE_MAJOR." '
  [ .[] | select((.version | startswith($p)) and (.files | index("linux-x64"))) ]
  | ([ .[] | select(.lts != false) ] + .) | first // empty
  | "\(.version) \(.lts)"' "$DL/node-index.json")
[ -n "$node_line" ] || die "no Node.js $NODE_MAJOR release for linux-x64 in index.json"
read -r NODE_VERSION NODE_LTS <<< "$node_line"
if [ "$NODE_LTS" = false ]; then echo "warning: Node.js $NODE_MAJOR has no LTS release yet; using $NODE_VERSION"; fi
NODE_DIST="node-$NODE_VERSION-linux-x64"
fetch "https://nodejs.org/dist/$NODE_VERSION/SHASUMS256.txt" "$DL/node-SHASUMS256.txt"
fetch "https://nodejs.org/dist/$NODE_VERSION/$NODE_DIST.tar.xz" "$DL/$NODE_DIST.tar.xz"
check_sha256 "$DL/$NODE_DIST.tar.xz" "$(awk -v f="$NODE_DIST.tar.xz" '$2 == f { print $1 }' "$DL/node-SHASUMS256.txt")"
mkdir -p /opt/node
find /opt/node -mindepth 1 -maxdepth 1 -name 'node-v*' -exec rm -rf {} +
tar -xJf "$DL/$NODE_DIST.tar.xz" -C /opt/node --no-same-owner
ln -sfn "$NODE_DIST" /opt/node/current
for b in /opt/node/current/bin/*; do ln -sfn "$b" "/usr/local/bin/${b##*/}"; done
# npm's global config: "npm install -g" goes to the user's ~/.local (bin is on
# PATH), so it works without sudo and with the user's own offline cache.
mkdir -p "/opt/node/$NODE_DIST/etc"
cat > "/opt/node/$NODE_DIST/etc/npmrc" <<'EOF'
# Global installs go under each user's own ~/.local (set by dev-toolkit).
prefix=${HOME}/.local
EOF
echo "Node.js $NODE_VERSION (LTS: $NODE_LTS) in /opt/node/$NODE_DIST"

# ------------------------------------------------------------------ .NET --
log ".NET SDK $DOTNET_CHANNEL"
DOTNET_META="https://builds.dotnet.microsoft.com/dotnet/release-metadata/$DOTNET_CHANNEL/releases.json"
fetch "$DOTNET_META" "$DL/dotnet-releases.json"
DOTNET_SDK=$(jq -r '."latest-sdk" // empty' "$DL/dotnet-releases.json")
DOTNET_SHA512=$(jq -r --arg v "$DOTNET_SDK" '
  [ .releases[] | (.sdks // [.sdk])[] | select(.version == $v) | .files[]
    | select(.rid == "linux-x64" and (.name | endswith(".tar.gz"))) | .hash ] | first // empty' \
  "$DL/dotnet-releases.json")
[ -n "$DOTNET_SDK" ] && [ -n "$DOTNET_SHA512" ] || die "no linux-x64 SDK for channel $DOTNET_CHANNEL in $DOTNET_META"
fetch https://dot.net/v1/dotnet-install.sh "$DL/dotnet-install.sh"
rm -rf /usr/share/dotnet
# dotnet-install.sh doesn't check hashes itself; it keeps the archive it
# unpacked, and that archive is checked against the release metadata.
bash "$DL/dotnet-install.sh" --version "$DOTNET_SDK" --install-dir /usr/share/dotnet --no-path \
  --zip-path "$DL/dotnet-sdk.tar.gz" --keep-zip
echo "$DOTNET_SHA512  $DL/dotnet-sdk.tar.gz" | sha512sum -c --quiet - \
  || { rm -rf /usr/share/dotnet; die "SHA-512 mismatch for the .NET SDK $DOTNET_SDK archive"; }
chown -R root:root /usr/share/dotnet
ln -sfn /usr/share/dotnet/dotnet /usr/local/bin/dotnet
echo ".NET SDK $DOTNET_SDK in /usr/share/dotnet (archive SHA-512 matches)"

# -------------------------------------------------------------------- uv --
log "uv"
if [ "$UV_VERSION" = latest ]; then
  UV_VERSION=$(curl --proto '=https' -fsSLI -o /dev/null -w '%{url_effective}' \
    https://github.com/astral-sh/uv/releases/latest | sed -E 's|.*/tag/||')
fi
[[ "$UV_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "could not work out the uv version (got '$UV_VERSION')"
UV_ASSET=uv-x86_64-unknown-linux-musl
UV_BASE="https://github.com/astral-sh/uv/releases/download/$UV_VERSION"
fetch "$UV_BASE/$UV_ASSET.tar.gz" "$DL/$UV_ASSET.tar.gz"
fetch "$UV_BASE/$UV_ASSET.tar.gz.sha256" "$DL/$UV_ASSET.tar.gz.sha256"
check_sha256 "$DL/$UV_ASSET.tar.gz" "$(awk '{ print $1; exit }' "$DL/$UV_ASSET.tar.gz.sha256")"
tar -xzf "$DL/$UV_ASSET.tar.gz" -C "$DL" --no-same-owner
install -m 0755 "$DL/$UV_ASSET/uv" "$DL/$UV_ASSET/uvx" /usr/local/bin/
echo "uv $UV_VERSION (static musl build) in /usr/local/bin"

# ---------------------------------------------------------------- Python --
log "Python $PYTHON_VERSION through uv, the default python for $DEV_USER"
# --default adds python and python3 next to python3.x in ~/.local/bin
as_dev uv python install "$PYTHON_VERSION" --default
as_dev uv cache clean

# ------------------------------------------------------------------ Rust --
log "Rust stable through rustup, for $DEV_USER"
RUSTUP_BASE=https://static.rust-lang.org/rustup/dist/x86_64-unknown-linux-gnu
fetch "$RUSTUP_BASE/rustup-init" "$DL/rustup-init"
fetch "$RUSTUP_BASE/rustup-init.sha256" "$DL/rustup-init.sha256"
check_sha256 "$DL/rustup-init" "$(awk '{ print $1; exit }' "$DL/rustup-init.sha256")"
chmod 0755 "$DL/rustup-init"
as_dev "$DL/rustup-init" -y --no-modify-path --profile minimal --default-toolchain stable \
  --component clippy,rustfmt,rust-src,rust-analyzer
# Offline there is nothing to update from, and a missing toolchain named in a
# rust-toolchain.toml should be an error rather than a download attempt.
as_dev rustup set auto-self-update disable
as_dev rustup set auto-install disable

# ------------------------------------------------------------ environment --
log "Environment"
cat > /etc/profile.d/dev-toolkit.sh <<'EOF'
# dev-toolkit: toolchain paths and offline defaults. Read by login shells, and
# by interactive bash through /etc/bash.bashrc.

# uv's Python (python, python3) and Rust's cargo bin ahead of the system ones;
# .NET global tools last.
case ":$PATH:" in *":$HOME/.cargo/bin:"*) ;; *) PATH="$HOME/.cargo/bin:$PATH" ;; esac
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) PATH="$HOME/.local/bin:$PATH" ;; esac
case ":$PATH:" in *":$HOME/.dotnet/tools:"*) ;; *) PATH="$PATH:$HOME/.dotnet/tools" ;; esac
export PATH

export DOTNET_ROOT=/usr/share/dotnet
export DOTNET_CLI_TELEMETRY_OPTOUT=1
export DOTNET_NOLOGO=1
export DOTNET_SKIP_FIRST_TIME_EXPERIENCE=1
export DOTNET_CLI_WORKLOAD_UPDATE_NOTIFY_DISABLE=1
# The first-use workload check needs network and write access to the SDK
# folder; without them it only prints a warning.
export DOTNET_SKIP_WORKLOAD_INTEGRITY_CHECK=true
# Package signature checks can't reach revocation lists offline.
export NUGET_CERT_REVOCATION_MODE=offline

# uv never goes to the network and never downloads Python builds.
export UV_OFFLINE=1
export UV_PYTHON_DOWNLOADS=never
EOF
chmod 0644 /etc/profile.d/dev-toolkit.sh
if ! grep -q 'profile.d/dev-toolkit.sh' /etc/bash.bashrc; then
  printf '\n# dev-toolkit: the same paths and settings for shells that are not login shells\n%s\n' \
    'if [ -r /etc/profile.d/dev-toolkit.sh ]; then . /etc/profile.d/dev-toolkit.sh; fi' >> /etc/bash.bashrc
fi

install -m 0755 "$LINK_SCRIPT" /usr/local/bin/dev-toolkit-link

# --------------------------------------------------------------- summary --
log "Installed"
dev_env() { as_dev bash -c '. /etc/profile.d/dev-toolkit.sh && "$@"' _ "$@"; }
echo "rustc:  $(dev_env rustc --version)"
echo "cargo:  $(dev_env cargo --version)"
echo "python: $(dev_env python --version) ($(dev_env sh -c 'command -v python'))"
echo "uv:     $(dev_env uv --version)"
echo "node:   $(dev_env node --version)   npm: $(dev_env npm --version)"
echo "dotnet: $(dev_env dotnet --version)"
echo "clang:  $(clang --version | head -1)"
echo "cmake:  $(cmake --version | head -1)"
