# Build the Windows AesApp Radio Updater .exe.
#
# Run from the python/ directory (the one containing bt_ota_gui_win.spec), in a
# copy of the repo that still has bt_ota\libjl_ota_auth.so and bt_ota\assets\*
# (they are gitignored, so use your working copy, not a fresh clone).
#
#   powershell -ExecutionPolicy Bypass -File build_windows.ps1 -Python "C:\path\to\python.exe"
#
# The .exe architecture matches the Python you pass. On Windows-on-ARM, install an
# x64 (or x86) python.org build and point -Python at it to cross-build via emulation.
param(
    [string]$Python = "python"
)
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

Write-Host "== Python ==" -ForegroundColor Cyan
& $Python -c "import platform,sys;print('arch:',platform.machine(),platform.architecture()[0]);print(sys.version)"

$venv = ".venv-win"
if (-not (Test-Path $venv)) { & $Python -m venv $venv }
$py = Join-Path $venv "Scripts\python.exe"

Write-Host "== Installing build deps ==" -ForegroundColor Cyan
& $py -m pip install --upgrade pip
# unicorn is NOT needed: the JieLi auth now runs in pure Python (bt_ota._jl_e1),
# because unicorn's JIT/memory setup access-violates inside the frozen app on
# hardened Windows. We neither install nor bundle it (the .exe spec excludes it).
# certifi ships the CA bundle radio_fw.download needs for HTTPS in the frozen app
# (a frozen Windows exe has no system CA store Python's ssl can see).
& $py -m pip install bleak pyserial pyinstaller pillow certifi

Write-Host "== Sanity: imports + pure-Python auth (no unicorn) ==" -ForegroundColor Cyan
$env:JL_OTA_AUTH_SO = (Resolve-Path "bt_ota\libjl_ota_auth.so")
& $py -c "import bleak; from bt_ota.jl_auth import AuthEmulator, _HAVE_UNICORN; print('auth sample (unicorn=%s):' % _HAVE_UNICORN, AuthEmulator().get_encrypted_auth_data(bytes([0]+list(range(1,17)))).hex())"

# On Windows-on-ARM, an emulated x64/x86 Python still reports platform.machine()
# == 'ARM64' (it reads PROCESSOR_ARCHITEW6432, which the emulator sets to the host
# arch). PyInstaller picks its bootloader from platform.machine(), so it would hunt
# for an arm64 bootloader that the amd64/win32 wheel doesn't ship. pip and the
# wheels themselves key off sysconfig.get_platform() (compiled in), which is
# correct -- so we align the process env to that before building.
$plat = (& $py -c "import sysconfig;print(sysconfig.get_platform())").Trim()
$arch = @{ "win-amd64" = "AMD64"; "win32" = "x86"; "win-arm64" = "ARM64" }[$plat]
if ($arch) {
    $env:PROCESSOR_ARCHITECTURE = $arch
    Remove-Item Env:\PROCESSOR_ARCHITEW6432 -ErrorAction SilentlyContinue
    Write-Host "== Arch: sysconfig=$plat -> PROCESSOR_ARCHITECTURE=$arch ==" -ForegroundColor Cyan
    & $py -c "import platform;print('   platform.machine() now:', platform.machine())"
}

Write-Host "== Building (PyInstaller onefile) ==" -ForegroundColor Cyan
& $py -m PyInstaller --noconfirm --clean bt_ota_gui_win.spec

$exe = "dist\AesApp Radio Updater.exe"
if (Test-Path $exe) {
    Write-Host "BUILT: $exe" -ForegroundColor Green
    Get-Item $exe | Select-Object Name,Length,LastWriteTime | Format-List
} else {
    Write-Error "build failed: $exe not found"
}
