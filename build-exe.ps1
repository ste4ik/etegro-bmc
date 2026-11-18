# Build the Windows release: PyInstaller onedir + bundled 32-bit JRE.
# Run from the repository root: powershell -ExecutionPolicy Bypass -File build-exe.ps1
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$python = (Get-Command python).Source
$build = Join-Path $env:TEMP "etegro-bmc-build"
$out = Join-Path $build "dist\etegro-bmc"

if (-not (Test-Path (Join-Path $root "runtime"))) {
    throw "runtime\ is missing. Run setup.py first so the 32-bit JRE is downloaded."
}
if (-not (Test-Path (Join-Path $root "app\decoder\classes\DecoderServer.class"))) {
    throw "decoder classes are missing. Compile app\decoder\DecoderServer.java with a 32-bit JDK."
}

# pyghmi's OEM plugin loader scans its package directory on disk, so that
# directory has to exist next to the frozen modules.
$pyghmiRoot = & $python -c "import os,pyghmi;print(os.path.dirname(pyghmi.__file__))"
$pyghmiOem = Join-Path $pyghmiRoot "ipmi\oem"
if (-not (Test-Path (Join-Path $pyghmiOem "lookup.py"))) {
    throw "pyghmi OEM plugins not found at $pyghmiOem"
}

Remove-Item -Recurse -Force $build -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force $build | Out-Null

& $python -m PyInstaller --noconfirm --clean --onedir --noconsole `
    --name etegro-bmc `
    --distpath (Join-Path $build "dist") `
    --workpath (Join-Path $build "work") `
    --specpath $build `
    --paths $root --paths (Join-Path $root "app") `
    --add-data "$root\app\templates;templates" `
    --add-data "$root\app\static\app.css;static" `
    --add-data "$root\app\static\app.js;static" `
    --add-data "$root\app\decoder;decoder" `
    --add-data "$pyghmiOem;pyghmi\ipmi\oem" `
    --hidden-import ipmi_worker --hidden-import esmskvm `
    --collect-submodules pyghmi `
    --collect-all webview `
    --collect-all clr_loader `
    --hidden-import clr `
    --exclude-module PyQt5 `
    --exclude-module PyQt6 `
    --exclude-module PySide2 `
    --exclude-module PySide6 `
    --exclude-module qtpy `
    --exclude-module matplotlib `
    --exclude-module IPython `
    --exclude-module jupyter `
    --exclude-module notebook `
    --exclude-module nbformat `
    --exclude-module zmq `
    (Join-Path $root "app\bmcapp.py")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

Copy-Item -Recurse (Join-Path $root "runtime") (Join-Path $out "runtime")
Copy-Item (Join-Path $root "README.md") (Join-Path $out "README.md")
Copy-Item (Join-Path $root "LICENSE") (Join-Path $out "LICENSE")

# Never ship screenshots or configs from the development tree.
Remove-Item -Recurse -Force (Join-Path $out "_internal\static\shots") -ErrorAction SilentlyContinue
Remove-Item -Force (Join-Path $out "config.json") -ErrorAction SilentlyContinue

$zipBase = Join-Path $root "etegro-bmc-windows-x64"
Remove-Item -Force "$zipBase.zip" -ErrorAction SilentlyContinue
& $python -c "import shutil,sys;shutil.make_archive(sys.argv[1],'zip',sys.argv[2])" $zipBase $out
if ($LASTEXITCODE -ne 0) { throw "Could not create the zip" }
Write-Host "Built $zipBase.zip"
