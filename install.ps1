# Install from a downloaded/extracted checkout. No WSL, Docker or global Java needed.
[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'

function Find-Python {
    $candidates = @()
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $candidates += (& py -3 -c 'import sys; print(sys.executable)' 2>$null)
    }
    if (Get-Command python -ErrorAction SilentlyContinue) {
        $candidates += (& python -c 'import sys; print(sys.executable)' 2>$null)
    }
    $candidates += "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path $candidate)) {
            & $candidate -c 'import sys; sys.exit(0 if sys.version_info >= (3,11,8) else 1)'
            if ($LASTEXITCODE -eq 0) { return $candidate }
        }
    }
    return $null
}

$python = Find-Python
if (!$python) {
    if (!(Get-Command winget -ErrorAction SilentlyContinue)) {
        throw 'Install Python 3.11.8+ from python.org (enable Python Launcher), then rerun install.ps1.'
    }
    & winget install --id Python.Python.3.12 --exact --scope user --silent --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) { throw 'Python installation failed.' }
    $python = Find-Python
    if (!$python) { throw 'Python was not found. Reopen PowerShell and rerun install.ps1.' }
}

$target = Join-Path $env:LOCALAPPDATA 'uwucraft-app'
$venv = Join-Path $target 'venv'
& $python -m venv $venv
if ($LASTEXITCODE -ne 0) { throw 'Cannot create Python environment.' }
$runtime = Join-Path $venv 'Scripts\python.exe'
& $runtime -m pip install --disable-pip-version-check --upgrade $PSScriptRoot
if ($LASTEXITCODE -ne 0) { throw 'uwucraft installation failed.' }

# A launcher independent of the original checkout location. No permanent policy changes.
$launcher = Join-Path $target 'uwucraft.cmd'
$exe = Join-Path $venv 'Scripts\uwucraft.exe'
[IO.File]::WriteAllText($launcher, "@echo off`r`n`"%~dp0venv\Scripts\uwucraft.exe`" %*`r`n", [Text.UTF8Encoding]::new($false))
$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
if (($userPath -split ';') -notcontains $target) {
    [Environment]::SetEnvironmentVariable('Path', "$target;$userPath", 'User')
}
if (($env:Path -split ';') -notcontains $target) { $env:Path = "$target;$env:Path" }
$desktop = [Environment]::GetFolderPath('Desktop')
if ($desktop) {
    $shell = New-Object -ComObject WScript.Shell
    $link = $shell.CreateShortcut((Join-Path $desktop 'uwucraft.lnk'))
    $link.TargetPath = $exe
    $link.WorkingDirectory = $target
    $link.Description = 'uwucraft Minecraft server manager'
    $link.Save()
}
Write-Host 'Installed. Run: uwucraft' -ForegroundColor Green
Write-Host 'Create: uwucraft create --quick --loader paper --port 25565 --accept-eula --start'
