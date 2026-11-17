param(
    [switch]$Browser,
    [switch]$Console
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python) {
    Write-Host "Python 3 is not on PATH." -ForegroundColor Red
    exit 1
}

$root = Split-Path $PSScriptRoot -Parent
$runtime = Join-Path $root "runtime"
$hasJava = Get-ChildItem -Path $runtime -Filter java.exe -Recurse -ErrorAction SilentlyContinue
$needsSetup = (-not (Test-Path (Join-Path $root "vendor\JavaRSM.jar"))) -or (-not $hasJava)
if ($needsSetup) {
    & $python (Join-Path $PSScriptRoot "setup.py")
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

if ($Browser) {
    & $python ".\bmcapp.py" --no-window
    exit
}

if ($Console) {
    & $python ".\bmcapp.py"
    exit
}

$pythonw = Join-Path (Split-Path $python) "pythonw.exe"
if (-not (Test-Path $pythonw)) {
    $pythonw = $python
}

Start-Process -FilePath $pythonw `
    -ArgumentList "`"$PSScriptRoot\bmcapp.py`"" `
    -WorkingDirectory $PSScriptRoot `
    -RedirectStandardOutput "$PSScriptRoot\app.log" `
    -RedirectStandardError "$PSScriptRoot\app.err.log"
