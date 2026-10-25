# Create "AgentOffice" shortcuts that start the desktop app (AgentOffice.pyw) with no console window.
#
#   powershell -ExecutionPolicy Bypass -File scripts\install_shortcut.ps1            # Desktop + Start menu
#   powershell -ExecutionPolicy Bypass -File scripts\install_shortcut.ps1 -Startup   # also start with Windows
#   powershell -ExecutionPolicy Bypass -File scripts\install_shortcut.ps1 -Remove    # remove all of them
#
# Needs the project's virtual environment (.venv) set up as in the README.
param([switch]$Startup, [switch]$Remove)

$root = Split-Path -Parent $PSScriptRoot
$pythonw = Join-Path $root ".venv\Scripts\pythonw.exe"
$app = Join-Path $root "AgentOffice.pyw"
$icon = Join-Path $root "app\ui\agentoffice.ico"
$places = @([Environment]::GetFolderPath("Desktop"), [Environment]::GetFolderPath("Programs"))
if ($Startup) { $places += [Environment]::GetFolderPath("Startup") }

if ($Remove) {
    foreach ($dir in @([Environment]::GetFolderPath("Desktop"), [Environment]::GetFolderPath("Programs"),
                       [Environment]::GetFolderPath("Startup"))) {
        $lnk = Join-Path $dir "AgentOffice.lnk"
        if (Test-Path $lnk) { Remove-Item $lnk; "removed $lnk" }
    }
    return
}
if (-not (Test-Path $pythonw)) { Write-Error "No virtual environment at $pythonw. Set it up first (see README)."; exit 1 }

$shell = New-Object -ComObject WScript.Shell
foreach ($dir in $places) {
    $lnk = $shell.CreateShortcut((Join-Path $dir "AgentOffice.lnk"))
    $lnk.TargetPath = $pythonw
    $lnk.Arguments = "`"$app`""
    $lnk.WorkingDirectory = $root
    $lnk.IconLocation = $icon
    $lnk.Description = "AgentOffice: your AI office team (Ctrl+Shift+Space)"
    $lnk.Save()
    "created $(Join-Path $dir 'AgentOffice.lnk')"
}
