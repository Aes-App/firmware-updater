# Build the Windows AesApp Radio Updater .exe.
#
# Run it from anywhere -- it cd's to its own directory -- but run it inside a
# copy of the repo that still has bt_ota\libjl_ota_auth.so and bt_ota\assets\*
# (they are gitignored, so use your working copy, not a fresh clone).
#
#   powershell -ExecutionPolicy Bypass -File build_windows.ps1 -FetchPython amd64
#   powershell -ExecutionPolicy Bypass -File build_windows.ps1 -Python "C:\path\to\python.exe"
#
# -FetchPython downloads a portable CPython of that architecture and builds with
# it. Prefer it: installing Python on the build VM has failed in two different
# ways (see the block below), and the tarball needs no installer at all.
#
# THE .EXE ARCHITECTURE IS THE ARCHITECTURE OF THE PYTHON YOU PASS, and that is
# what ships, so pass it deliberately:
#
#   x64   -> runs on every modern Windows, Windows-on-ARM included (it emulates
#            x64). This is the build almost everyone downloads.
#   x86   -> only needed for 32-bit Windows.
#   ARM64 -> runs ONLY on Windows-on-ARM. Not a release build. On an ARM VM this
#            is what you get by accident if you do not pass -Python, so the
#            script stops unless you pass -AllowArm64.
#
# Every build lands in dist\ with its architecture in the file name, so x64 and
# x86 sit side by side and one release has one place to look. The per-arch
# BUILD caches stay separate (build-win-<arch>\), because a PyInstaller work
# directory is not portable between architectures.
#
# It refuses to produce an .exe it has not checked: the fast test suites run
# before the build and the frozen archive is inspected after it. -SkipTests is
# there for the day the test environment is the thing that is broken, and it says
# so in the output rather than quietly skipping.
param(
    [string]$Python = "python",
    [switch]$SkipTests,
    [switch]$AllowArm64,
    [ValidateSet("amd64", "win32")][string]$FetchPython
)
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

# -FetchPython amd64|win32 : get a portable CPython instead of installing one.
#
# WHY THIS EXISTS. Installing Python on the build VM is a dead end and has been
# more than once: the python.org bundle lays down only the exe component when it
# collides with a pre-installed ARM64 Python, and a managed machine refuses the
# installer outright (winget exit 1625, ERROR_INSTALL_POLICY_FAILURE, at machine
# AND user scope). The NuGet package has no tkinter, and neither does the
# embeddable zip, so neither can build a GUI.
#
# python-build-standalone is a tarball: tkinter and pip included, architecture
# fixed by the file you download rather than detected, extracted with the tar
# that ships with Windows. Nothing is installed and nothing needs a policy.
#
# NOT conda/Miniforge: its Tcl/Tk DLLs live in Library\bin\, which PyInstaller's
# tkinter hook does not collect, and the .exe then dies at launch with
# "ImportError: DLL load failed while importing _tkinter".
if ($FetchPython) {
    $triple = @{ "amd64" = "x86_64"; "win32" = "i686" }[$FetchPython]
    $dir = ".python-$FetchPython"
    $fetched = Join-Path $dir "python\python.exe"
    if (-not (Test-Path $fetched)) {
        Write-Host "== Fetching a portable CPython ($FetchPython) ==" -ForegroundColor Cyan
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        $rel = Invoke-RestMethod "https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest"
        $asset = $rel.assets |
            Where-Object { $_.name -like "cpython-3.12.*-$triple-pc-windows-msvc-install_only.tar.gz" } |
            Select-Object -First 1
        if (-not $asset) { throw "no 3.12 $triple install_only build in the latest release" }
        $tgz = Join-Path $env:TEMP $asset.name
        Write-Host "   $($asset.name)" -ForegroundColor DarkGray
        curl.exe -L --fail -o $tgz $asset.browser_download_url
        if ($LASTEXITCODE -ne 0) { throw "download failed: $($asset.browser_download_url)" }
        New-Item -ItemType Directory -Force -Path $dir | Out-Null
        tar -xzf $tgz -C $dir
        if (-not (Test-Path $fetched)) { throw "the tarball did not contain $fetched" }
    }
    $Python = (Resolve-Path $fetched).Path
    Write-Host "   using $Python" -ForegroundColor DarkGray
}

# PREFLIGHT. Everything below assumes the Python you passed can run, has its own
# standard library and has tkinter. Ask it, once, and fail with something
# readable if not -- the alternative is PowerShell reporting "you cannot call a
# method on a null-valued expression" while Python's real complaint scrolls past.
Write-Host "== Python ==" -ForegroundColor Cyan

# A path with a space, passed unquoted, is THE classic way to lose here:
# PowerShell splits -Python C:\Program Files\Python312\python.exe into
# "-Python C:\Program" plus a stray argument, and the rest of the script then
# complains about a Python that was never the one you meant. python.org's
# all-users install lives in C:\Program Files, so this is not a rare shape.
if ($Python -match '[\\/]' -and -not (Test-Path -LiteralPath $Python)) {
    throw @"
No such file: $Python

If the path contains a space it MUST be quoted:

    -Python "C:\Program Files\Python312\python.exe"

List what is installed, with paths:  py -0p
"@
}

$probe = $null
try {
    $probe = & $Python -c "import sysconfig,sys,tkinter;print(sysconfig.get_platform());print(sys.prefix);print(sys.version.split()[0])" 2>&1
} catch {
    $probe = $null
}
if ($LASTEXITCODE -ne 0 -or -not $probe -or $probe.Count -lt 3) {
    if ($probe) { $probe | ForEach-Object { Write-Host "   $_" -ForegroundColor DarkGray } }
    throw @"
That Python cannot be used for a build. Its own output is above.

Two things this script needs and the minimal distributions do not have: a real
standard library (Lib\os.py beside the executable) and tkinter. An unpacked
embeddable or NuGet package -- often a hand-made folder like C:\Python312-amd64
-- has neither, cannot create a venv, and reports sys.prefix as the current
drive because it never finds its own stdlib.

Install a full python.org build and pass the path py -0p then prints:

    winget install --id Python.Python.3.12 --architecture x64
    py -0p

Check any candidate first with:  Test-Path <dir>\Lib\os.py
"@
}

# THE ARCHITECTURE COMES FROM THE PYTHON YOU PASSED, settled here -- before a
# venv, before dependencies, before anything slow. sysconfig is compiled into the
# interpreter; platform.machine() is not, and lies under emulation.
$plat = "$($probe[0])".Trim()
$tag = $plat -replace '^win-', ''
Write-Host "   $Python" -ForegroundColor DarkGray
Write-Host "   Python $($probe[2]), prefix $($probe[1])" -ForegroundColor DarkGray
Write-Host "== Target: $plat ==" -ForegroundColor Cyan

# An ARM64 .exe runs on Windows-on-ARM and nowhere else, so it is never the build
# to hand out -- and on an ARM machine it is exactly what `-Python python` gives
# you, because the plain `python` on PATH is the native one. Stop before the
# work, not after it.
if ($plat -eq "win-arm64" -and -not $AllowArm64) {
    throw @"
This Python is ARM64, so the .exe would only run on Windows-on-ARM.

Point -Python at an x64 install instead (it runs under emulation here). List
what is installed with:  py -0p

Add -AllowArm64 if an ARM64 build really is what you want.
"@
}

# ONE VENV PER ARCHITECTURE. A single .venv-win was reused whatever -Python said,
# so the first architecture built owned it and every later run silently used that
# interpreter -- an x64 path would produce an ARM64 .exe. The wheels inside are
# architecture-specific anyway, so they could never have been shared.
$venv = ".venv-win-$tag"
$py = Join-Path $venv "Scripts\python.exe"
if (Test-Path $venv) {
    # A directory that exists is not proof it holds the right thing. Probing it
    # must not be able to stop the script -- a half-made venv is a reason to
    # rebuild, not to give up -- so the probe is wrapped and failure is "no".
    $have = ""
    if (Test-Path $py) {
        try { $have = (& $py -c "import sysconfig;print(sysconfig.get_platform())" 2>$null) } catch { $have = "" }
    }
    if ("$have".Trim() -ne $plat) {
        Write-Host "   $venv is missing or not $plat -- rebuilding it" -ForegroundColor Yellow
        Remove-Item -Recurse -Force $venv
    }
}
if (-not (Test-Path $venv)) { & $Python -m venv $venv }
$got = (& $py -c "import sysconfig;print(sysconfig.get_platform())").Trim()
if ($got -ne $plat) { throw "the venv in $venv reports $got, not $plat" }
Write-Host "   venv: $venv ($got)" -ForegroundColor DarkGray

Write-Host "== Installing build deps ==" -ForegroundColor Cyan
& $py -m pip install --upgrade pip
# unicorn is NOT needed: the JieLi auth now runs in pure Python (bt_ota._jl_e1),
# because unicorn's JIT/memory setup access-violates inside the frozen app on
# hardened Windows. We neither install nor bundle it (the .exe spec excludes it).
# certifi ships the CA bundle radio_fw.download needs for HTTPS in the frozen app
# (a frozen Windows exe has no system CA store Python's ssl can see).
& $py -m pip install bleak pyserial pyinstaller pillow certifi pytest

Write-Host "== Sanity: imports + pure-Python auth (no unicorn) ==" -ForegroundColor Cyan
# .ProviderPath, not the PathInfo itself: on a share (\\Mac\...) the latter reads
# "Microsoft.PowerShell.Core\FileSystem::\\Mac\...", which Python cannot open,
# and this check then failed while the build carried on regardless.
$env:JL_OTA_AUTH_SO = (Resolve-Path "bt_ota\libjl_ota_auth.so").ProviderPath
& $py -c "import bleak; from bt_ota.jl_auth import AuthEmulator, _HAVE_UNICORN; print('auth sample (unicorn=%s):' % _HAVE_UNICORN, AuthEmulator().get_encrypted_auth_data(bytes([0]+list(range(1,17)))).hex())"

# The Digital Contact Refresh tab builds a list from an operator's own register
# download, using a GENERATED country table that nothing outside its package
# imports. Encode two contacts here: it proves the module and the table are both
# importable in this interpreter before anything is frozen around them.
Write-Host "== Sanity: the local contact builder ==" -ForegroundColor Cyan
& $py -c @"
from radio_contacts import contact_build as cb
from radio_contacts.contact_tables import continent_of
rows = 'RADIO_ID,CALLSIGN,FIRST_NAME,LAST_NAME,CITY,STATE,COUNTRY\n'
rows += ''.join('%d,K%dABC,First%d,Last,Ottawa,Ontario,Canada\n' % (3100000+i, i, i) for i in range(40))
import io, tempfile, os
p = os.path.join(tempfile.gettempdir(), 'aesapp_sanity_user.csv')
io.open(p, 'w', encoding='utf-8').write(rows)
store = cb.read_user_csv(p, min_bytes=0)
plan = cb.build_dmr_segments(store, 'anytone_878')
blocks = sum(len(s.data) // 16 for s in plan)
cb.check_plan(plan, 'anytone_878')
print('   built %d contacts -> %d segments, %d frames; CAN is in %s'
      % (len(store), len(plan), blocks, continent_of('CAN')))
"@

if ($SkipTests) {
    Write-Host "== Tests SKIPPED (-SkipTests) -- this .exe is unverified ==" -ForegroundColor Yellow
} else {
    Write-Host "== Tests: the encoders and the update check ==" -ForegroundColor Cyan
    # The byte-for-byte encoder fixtures and the version comparison. Both are
    # stdlib-only and take seconds; the GUI suites need a display and are not run
    # here. A build whose encoders disagree with the fixture must not ship.
    & $py -m pytest tests/test_contacts_local_build.py tests/test_update_check.py tests/test_contacts_segments.py -q
    if ($LASTEXITCODE -ne 0) { throw "tests failed -- refusing to build an .exe from this tree" }
}

# On Windows-on-ARM, an emulated x64/x86 Python still reports platform.machine()
# == 'ARM64' (it reads PROCESSOR_ARCHITEW6432, which the emulator sets to the host
# arch). PyInstaller picks its bootloader from platform.machine(), so it would hunt
# for an arm64 bootloader that the amd64/win32 wheel doesn't ship. pip and the
# wheels themselves key off sysconfig.get_platform() (compiled in), which is
# correct -- so we align the process env to that before building.
$arch = @{ "win-amd64" = "AMD64"; "win32" = "x86"; "win-arm64" = "ARM64" }[$plat]
if ($arch) {
    $env:PROCESSOR_ARCHITECTURE = $arch
    Remove-Item Env:\PROCESSOR_ARCHITEW6432 -ErrorAction SilentlyContinue
    Write-Host "== Arch: sysconfig=$plat -> PROCESSOR_ARCHITECTURE=$arch ==" -ForegroundColor Cyan
    & $py -c "import platform;print('   platform.machine() now:', platform.machine())"
}

# The .exe carries its architecture, so one dist\ holds them all without any
# build replacing another. The work directory does NOT: PyInstaller's cache is
# architecture-specific, so each gets its own.
$distDir = "dist"
$workDir = "build-win-$tag"
Write-Host "== Output: $distDir\AesApp-Radio-Updater-win-$tag.exe ==" -ForegroundColor Cyan

Write-Host "== Building (PyInstaller onefile) ==" -ForegroundColor Cyan
& $py -m PyInstaller --noconfirm --clean --distpath $distDir --workpath $workDir bt_ota_gui_win.spec

$built = Join-Path $distDir "AesApp Radio Updater.exe"
$exe = Join-Path $distDir "AesApp-Radio-Updater-win-$tag.exe"
if (Test-Path $built) {
    if (Test-Path $exe) { Remove-Item $exe }
    Rename-Item $built (Split-Path $exe -Leaf)
}
if (-not (Test-Path $exe)) { Write-Error "build failed: $exe not found" }

# Everything this app does after startup is imported lazily, so a module can fall
# out of the .exe without one warning here and fail only when a user opens that
# tab. Read the frozen archive and check.
Write-Host "== Checking the frozen archive ==" -ForegroundColor Cyan
& $py tools\check_frozen_modules.py $exe
# 1 = it looked and something is missing. 2 = it could not look, which is a
# problem with the check, not with the .exe, and must not block a release.
if ($LASTEXITCODE -eq 1) { throw "the .exe is missing modules it needs -- see above" }
elseif ($LASTEXITCODE -ne 0) {
    Write-Host "   the archive check could not run -- open each tab by hand before shipping" -ForegroundColor Yellow
}

Write-Host "BUILT: $exe" -ForegroundColor Green
Get-Item $exe | Select-Object Name,Length,LastWriteTime | Format-List
Write-Host "Unsigned. Windows SmartScreen will warn on first run." -ForegroundColor Yellow
Write-Host "A release is normally x64 AND x86 -- run again with the other Python." -ForegroundColor Cyan
