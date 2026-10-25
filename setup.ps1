# One-time setup for AgentOffice on Windows. Safe to run again: it reuses what is there and never
# overwrites your .env.
#
#   powershell -ExecutionPolicy Bypass -File setup.ps1              # install, .env, Desktop + Start menu shortcut
#   powershell -ExecutionPolicy Bypass -File setup.ps1 -Dev         # also install pytest and run the tests
#   powershell -ExecutionPolicy Bypass -File setup.ps1 -Startup     # also start AgentOffice with Windows
#   powershell -ExecutionPolicy Bypass -File setup.ps1 -NoShortcut  # skip the shortcuts
#
# Needs Python 3.11 or newer (python.org or the Python install manager) and Microsoft Edge or Google Chrome.
param([switch]$Dev, [switch]$Startup, [switch]$NoShortcut)

$root = $PSScriptRoot
$venvPython = Join-Path $root ".venv\Scripts\python.exe"
$minimum = [version]"3.11"

function Step($text) { Write-Host "==> $text" -ForegroundColor Cyan }
function Fail($text) { Write-Host "Setup failed: $text" -ForegroundColor Red; exit 1 }

function Get-PythonVersion($exe, $prefix) {
    # Prints nothing for the Microsoft Store "python" alias, which is not a real interpreter.
    $out = & $exe @prefix -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $out) { return $null }
    return [version]"$out"
}

# 1. A Python interpreter that is new enough: the py launcher first, then python on PATH.
$python = $null
foreach ($candidate in "py -3", "python", "python3") {
    $parts = $candidate -split " "
    if (-not (Get-Command $parts[0] -ErrorAction SilentlyContinue)) { continue }
    $prefix = @($parts | Select-Object -Skip 1)
    $version = Get-PythonVersion $parts[0] $prefix
    if ($version -and $version -ge $minimum) { $python = @{ Exe = $parts[0]; Prefix = $prefix; Version = $version }; break }
}

# 2. The virtual environment in .venv.
if (Test-Path $venvPython) {
    $version = Get-PythonVersion $venvPython @()
    if (-not $version -or $version -lt $minimum) {
        Fail ".venv uses Python $version but AgentOffice needs $minimum or newer. Delete the .venv folder and run setup again."
    }
    Step "Using the existing virtual environment (.venv, Python $version)"
} else {
    if (-not $python) {
        Fail "Python $minimum or newer was not found. Install it from https://www.python.org/downloads/ and run setup again."
    }
    Step "Creating the virtual environment (.venv) with Python $($python.Version)"
    & $python.Exe @($python.Prefix) -m venv (Join-Path $root ".venv")
    if ($LASTEXITCODE -ne 0) { Fail "could not create .venv (see the error above)." }
}

# 3. Dependencies.
$requirements = if ($Dev) { "requirements-dev.txt" } else { "requirements.txt" }
Step "Installing dependencies from $requirements"
& $venvPython -m pip install --disable-pip-version-check --quiet -r (Join-Path $root $requirements)
if ($LASTEXITCODE -ne 0) { Fail "pip could not install the dependencies (see the error above)." }

# 4. A private .env, created once from the placeholders.
$envFile = Join-Path $root ".env"
if (Test-Path $envFile) {
    Step ".env already exists; leaving it as it is"
} else {
    Copy-Item (Join-Path $root ".env.example") $envFile
    Step "Created .env from .env.example (it holds placeholders only)"
}

# 5. Which mode AgentOffice will run in with this .env.
Push-Location $root
$mode = & $venvPython -c "import fastapi, uvicorn, httpx, openpyxl; from app.config import load_settings; s = load_settings(); m = s.missing_for_default_mode(); print('forced' if s.sample_mode else ' '.join(m) or 'ready')"
$status = $LASTEXITCODE
Pop-Location
if ($status -ne 0) { Fail "AgentOffice could not load its settings (see the error above; check the values in .env)." }
if ($mode -eq "forced") {
    Step "Mode: sample mode (SAMPLE_MODE=true in .env)"
} elseif ($mode -eq "ready") {
    Step "Mode: default mode (Zetaris + model provider configured). Test it: .venv\Scripts\python.exe scripts\check_connections.py --detect"
} else {
    Step "Mode: sample mode, no keys needed. For default mode, set these in .env: $($mode -replace ' ', ', ')"
}

# 6. Optional: the test suite.
if ($Dev) {
    Step "Running the tests"
    Push-Location $root
    & $venvPython -m pytest -q
    $status = $LASTEXITCODE
    Pop-Location
    if ($status -ne 0) { Fail "some tests failed (see above)." }
}

# 7. Shortcuts that start the desktop app with no console window.
if (-not $NoShortcut) {
    Step "Creating the AgentOffice shortcuts"
    & (Join-Path $root "scripts\install_shortcut.ps1") -Startup:$Startup
    if ($LASTEXITCODE -ne 0) { Fail "could not create the shortcuts (see the error above)." }
}

Write-Host ""
Write-Host "AgentOffice is ready." -ForegroundColor Green
if (-not $NoShortcut) {
    Write-Host "  Desktop app : open AgentOffice from the Desktop or Start menu, then press Ctrl+Shift+Space"
} else {
    Write-Host "  Desktop app : .venv\Scripts\pythonw.exe AgentOffice.pyw   (then press Ctrl+Shift+Space)"
}
Write-Host "  Browser     : .venv\Scripts\python.exe run.py   then open http://localhost:8000"
Write-Host "  Guide       : docs\SETUP.md"
