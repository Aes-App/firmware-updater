#!/usr/bin/env bash
# Build the Linux AesApp Radio Updater: one self-contained executable, shipped in
# a .tar.gz with the udev rule the serial tabs want.
#
# Run it from anywhere -- it cd's to its own directory -- but run it inside a
# copy of the repo that still has bt_ota/libjl_ota_auth.so and bt_ota/assets/*
# (they are gitignored, so use your working copy, not a fresh clone).
#
#   ./build_linux.sh --docker x86_64             # from macOS or Linux, with Docker
#   ./build_linux.sh --docker aarch64
#   ./build_linux.sh --python /usr/bin/python3.12  # on a Linux box, with that Python
#
# BUILD ON UBUNTU 22.04, which is what --docker does. Three things come from the
# machine the binary is built on, and nothing else decides them:
#
#   the architecture -- x86_64 is almost every Linux PC; aarch64 is a Raspberry
#     Pi 4/5 on a 64-bit OS, an ARM laptop, or an ARM VM on a Mac.
#   the oldest glibc it starts on -- the build machine's. Ubuntu 22.04 has 2.35,
#     and Debian 12, Raspberry Pi OS bookworm, Mint 21 and every later Ubuntu
#     have that or newer. Built on a rolling release (Kali, Arch) the binary
#     refuses to start on all of them. The script reads the floor out of the
#     finished binary and prints it.
#   Tk -- it must be built with Xft, as a distro's is. python-build-standalone's
#     is not: its window draws in X core fonts, where the default family resolves
#     to a typewriter face and "…" to the wrong glyphs. The preflight refuses a
#     Python whose Tk has no Xft.
#
# Python 3.12, the version the Windows build uses, comes from the deadsnakes PPA
# with its tkinter on Ubuntu's own Tk 8.6. The desktop's fonts, fontconfig and
# X11 libraries are deliberately not bundled (see bt_ota_gui_linux.spec).
#
# It refuses to produce a binary it has not checked: the fast test suites run
# before the build, the frozen archive is inspected after it, and the binary's
# own self-test (BT_OTA_DIAG=1) runs last. --skip-tests is there for the day the
# test environment is the thing that is broken, and it says so in the output.
set -euo pipefail
cd "$(dirname "$0")"

PYTHON=""
DOCKER=""
SKIP_TESTS=0
while [ $# -gt 0 ]; do
    case "$1" in
        --python)     PYTHON="${2:?--python needs a path}"; shift 2 ;;
        --docker)     DOCKER="${2:?--docker needs x86_64 or aarch64}"; shift 2 ;;
        --skip-tests) SKIP_TESTS=1; shift ;;
        -h|--help)    sed -n '2,/^set -euo/{/^set -euo/d;s/^# \{0,1\}//;p;}' "$0"; exit 0 ;;
        *)            echo "unknown argument: $1 (see --help)" >&2; exit 2 ;;
    esac
done

step() { printf '\033[36m== %s ==\033[0m\n' "$*"; }
note() { printf '\033[90m   %s\033[0m\n' "$*"; }
warn() { printf '\033[33m%s\033[0m\n' "$*"; }
die()  { printf '\033[31m%s\033[0m\n' "$*" >&2; exit 1; }

[ -f bt_ota/libjl_ota_auth.so ] || die "bt_ota/libjl_ota_auth.so is missing -- build from a working copy, not a fresh clone.
Recreate it with:  python -m bt_ota.extract_auth_lib \"OTA V1.7.2.apk\""

# --docker <arch>: this same script, inside an Ubuntu 22.04 image of that
# architecture with the repo mounted. The image is built once and cached by
# Docker. Runs as the calling user, so what it writes into the repo is theirs.
if [ -n "$DOCKER" ]; then
    case "$DOCKER" in
        x86_64)  platform=linux/amd64 ;;
        aarch64) platform=linux/arm64 ;;
        *)       die "--docker takes x86_64 or aarch64, not $DOCKER" ;;
    esac
    image="aesapp-radio-updater-build:ubuntu22.04-$DOCKER"
    step "Build image $image"
    docker build --quiet --platform "$platform" -t "$image" - <<'EOF' >/dev/null
FROM ubuntu:22.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends software-properties-common gpg-agent binutils ca-certificates \
 && add-apt-repository -y ppa:deadsnakes/ppa \
 && apt-get update \
 && apt-get install -y --no-install-recommends python3.12 python3.12-venv python3.12-tk libpython3.12 \
 && rm -rf /var/lib/apt/lists/*
EOF
    skip=""
    [ "$SKIP_TESTS" = 1 ] && skip="--skip-tests"
    step "Building in $image"
    exec docker run --rm --platform "$platform" --user "$(id -u):$(id -g)" -e HOME=/tmp \
        -v "$(pwd):/src" -w /src "$image" ./build_linux.sh --python /usr/bin/python3.12 $skip
fi

[ -n "$PYTHON" ] || die "pass --docker x86_64|aarch64, or --python PATH on a Linux box (see --help)"

# PREFLIGHT. Everything below assumes this Python runs, has its own standard
# library and has tkinter on a Tk with Xft. Ask it once, and fail with
# something readable if not.
step "Python"
if ! probe=$("$PYTHON" -c 'import sysconfig, sys, tkinter, _tkinter
print(sysconfig.get_platform()); print(sys.prefix); print(sys.version.split()[0]); print(tkinter.TkVersion); print(_tkinter.__file__)' 2>&1); then
    printf '%s\n' "$probe" | sed 's/^/   /'
    die "That Python cannot be used for a build. Its own output is above.

A 'No module named tkinter' means a distro Python without its Tk package
(python3.12-tk from deadsnakes on Ubuntu) -- or use --docker, which has it."
fi
plat=$(sed -n 1p <<<"$probe")
prefix=$(sed -n 2p <<<"$probe")
version=$(sed -n 3p <<<"$probe")
tag=${plat#linux-}
note "$PYTHON"
note "Python $version, Tk $(sed -n 4p <<<"$probe"), prefix $prefix"
if ! ldd "$(sed -n 5p <<<"$probe")" 2>/dev/null | grep -q 'libXft\.so'; then
    die "This Python's Tk is built without Xft: the window would draw in X core fonts,
with headings in a typewriter face and some characters as the wrong glyphs.
Use a distro Python's tkinter (python3.12-tk on Ubuntu), or --docker."
fi
step "Target: $plat"
case "$plat" in
    linux-x86_64|linux-aarch64) ;;
    *) die "this Python is $plat; the Linux build is x86_64 or aarch64" ;;
esac

# ONE VENV PER ARCHITECTURE, and it must be made from THIS Python. Two Pythons
# can share a platform and a prefix (/usr for every distro Python), so the check
# is all three -- a venv another Python made would build with that Python, and
# the binary would quietly inherit its glibc floor and its Tk.
venv=".venv-linux-$tag"
py="$venv/bin/python"
if [ -d "$venv" ]; then
    have=$("$py" -c 'import sys, sysconfig; print(sysconfig.get_platform()); print(sys.base_prefix); print(sys.version.split()[0])' 2>/dev/null || true)
    if [ "$have" != "$plat"$'\n'"$prefix"$'\n'"$version" ]; then
        warn "   $venv is missing, not $plat, or not made from this Python -- rebuilding it"
        rm -rf "$venv"
    fi
fi
[ -d "$venv" ] || "$PYTHON" -m venv "$venv"
note "venv: $venv"

step "Installing build deps"
"$py" -m pip install --quiet --upgrade pip
# No unicorn and no pillow: the JieLi auth is pure Python, and a Linux binary
# carries no icon resource for pillow to convert. certifi ships the CA bundle
# radio_fw.download needs for HTTPS in the frozen app.
"$py" -m pip install --quiet bleak pyserial pyinstaller certifi pytest

step "Sanity: imports + pure-Python auth (no unicorn)"
JL_OTA_AUTH_SO="$(pwd)/bt_ota/libjl_ota_auth.so" "$py" -c "import bleak; from bt_ota.jl_auth import AuthEmulator, _HAVE_UNICORN; print('   auth sample (unicorn=%s):' % _HAVE_UNICORN, AuthEmulator().get_encrypted_auth_data(bytes([0]+list(range(1,17)))).hex())"

# The Digital Contact Refresh tab builds a list from an operator's own register
# download, using a GENERATED country table that nothing outside its package
# imports. Encode a few contacts here: it proves the module and the table are
# both importable in this interpreter before anything is frozen around them.
step "Sanity: the local contact builder"
"$py" - <<'EOF'
from radio_contacts import contact_build as cb
from radio_contacts.contact_tables import continent_of
import os, tempfile
rows = 'RADIO_ID,CALLSIGN,FIRST_NAME,LAST_NAME,CITY,STATE,COUNTRY\n'
rows += ''.join('%d,K%dABC,First%d,Last,Ottawa,Ontario,Canada\n' % (3100000+i, i, i) for i in range(40))
fd, p = tempfile.mkstemp(suffix='.csv')
with os.fdopen(fd, 'w', encoding='utf-8') as f:
    f.write(rows)
try:
    store = cb.read_user_csv(p, min_bytes=0)
finally:
    os.unlink(p)
plan = cb.build_dmr_segments(store, 'anytone_878')
blocks = sum(len(s.data) // 16 for s in plan)
cb.check_plan(plan, 'anytone_878')
print('   built %d contacts -> %d segments, %d frames; CAN is in %s'
      % (len(store), len(plan), blocks, continent_of('CAN')))
EOF

if [ "$SKIP_TESTS" = 1 ]; then
    warn "== Tests SKIPPED (--skip-tests) -- this binary is unverified =="
else
    step "Tests: the encoders, the update check, the launch link, the port table"
    # Stdlib-only suites that take seconds; the GUI suites need a display and are
    # not run here. A build whose encoders disagree with the fixture must not ship.
    "$py" -m pytest -q -p no:cacheprovider tests/test_contacts_local_build.py tests/test_update_check.py \
        tests/test_contacts_segments.py tests/test_contacts_launch.py tests/test_ports.py \
        || die "tests failed -- refusing to build a binary from this tree"
fi

# The binary carries its architecture in its name, so one dist/ holds them all.
# The work directory does not: PyInstaller's cache is architecture-specific.
name="AesApp-Radio-Updater-linux-$tag"
stage="dist/$name"
step "Building (PyInstaller onefile) -> dist/$name.tar.gz"
"$py" -m PyInstaller --noconfirm --clean --distpath dist --workpath "build-linux-$tag" bt_ota_gui_linux.spec
[ -f dist/aesapp-radio-updater ] || die "build failed: dist/aesapp-radio-updater not found"
rm -rf "$stage"; mkdir -m 0755 "$stage"
# Explicit modes: the tarball would otherwise carry whatever umask the build ran under.
install -m 0755 dist/aesapp-radio-updater "$stage/aesapp-radio-updater"
install -m 0644 60-aesapp-radio.rules "$stage/"
rm -f dist/aesapp-radio-updater
bin="$stage/aesapp-radio-updater"

# Everything this app does after startup is imported lazily, so a module can fall
# out of the binary without one warning here and fail only when a user opens that
# tab. Read the frozen archive and check.
step "Checking the frozen archive"
rc=0; "$py" tools/check_frozen_modules.py "$bin" || rc=$?
# 1 = it looked and something is missing. 2 = it could not look, which is a
# problem with the check, not with the binary, and must not block a release.
if [ "$rc" = 1 ]; then die "the binary is missing modules it needs -- see above"
elif [ "$rc" != 0 ]; then warn "   the archive check could not run -- open each tab by hand before shipping"
fi

# Unlike the .exe, this one can be run where it was built: the self-test imports
# every tab, the BlueZ backend and the step photos without opening a window.
step "Self-test of the frozen binary (BT_OTA_DIAG=1)"
if ! BT_OTA_DIAG=1 AESAPP_NO_UPDATE_CHECK=1 "$bin" 2>&1 | sed 's/^/   /'; then
    die "the frozen binary failed its own self-test"
fi

# The oldest glibc this binary starts on is the newest symbol version anything
# inside it asks for: the bootloader, and every library it unpacks at launch.
step "glibc floor"
if command -v objdump >/dev/null; then
    scratch=$(mktemp -d)
    "$py" - "$bin" "$scratch" <<'EOF'
import os, sys
from PyInstaller.archive.readers import CArchiveReader
arch = CArchiveReader(sys.argv[1])
for n in arch.toc:
    if ".so" in os.path.basename(n):
        out = os.path.join(sys.argv[2], n.replace("/", "_"))
        with open(out, "wb") as f:
            f.write(arch.extract(n))
EOF
    floor=""; culprit=""
    for f in "$bin" "$scratch"/*; do
        # || true: a library that asks for no versioned glibc symbol makes grep fail
        v=$(objdump -T "$f" 2>/dev/null | grep -oE 'GLIBC_[0-9]+\.[0-9]+' | sed 's/^GLIBC_//' | sort -uV | tail -n 1) || true
        [ -n "$v" ] || continue
        if [ -z "$floor" ] || [ "$(printf '%s\n%s\n' "$floor" "$v" | sort -V | tail -n 1)" != "$floor" ]; then
            floor=$v; culprit=${f##*/}
        fi
    done
    rm -rf "$scratch"
    note "starts on glibc $floor or newer, set by $culprit"
    note "(Ubuntu 22.04: 2.35, Debian 12: 2.36, Ubuntu 24.04: 2.39)"
    if [ "$(printf '%s\n2.35\n' "$floor" | sort -V | tail -n 1)" != 2.35 ]; then
        warn "   Newer than Ubuntu 22.04's 2.35: this binary will not start on Ubuntu 22.04, Debian 12"
        warn "   or Raspberry Pi OS bookworm. Build with --docker for one that does."
    fi
else
    warn "   objdump not found (binutils) -- the glibc floor was not checked"
fi

# --owner/--group: GNU tar records the builder's user name in every header otherwise.
tar -C dist --owner=0 --group=0 --numeric-owner -czf "dist/$name.tar.gz" "$name"

printf '\033[32mBUILT: dist/%s.tar.gz\033[0m\n' "$name"
ls -l "dist/$name.tar.gz" "$bin" | sed 's/^/   /'
note "unpacked for testing in $stage/"
warn "Unsigned. A release is x86_64, and aarch64 for the Raspberry Pi -- run again for the other one."
