# Native Windows installer. Release ZIP includes the signed official Python installer.
[CmdletBinding()]
param([switch]$BootstrapPython)
$ErrorActionPreference = 'Stop'
$target = Join-Path $env:LOCALAPPDATA 'uwucraft-app'

function Test-Python([string]$Executable) {
    if (!$Executable -or !(Test-Path $Executable)) { return $false }
    try {
        & $Executable -c 'import sys, venv; sys.exit(0 if sys.version_info >= (3,11,8) and sys.maxsize > 2**32 else 1)' 2>$null
        return $LASTEXITCODE -eq 0
    } catch { return $false }
}

function Find-Python {
    $candidates = @((Join-Path $target 'python\python.exe'))
    foreach ($command in @('py', 'python')) {
        if (Get-Command $command -ErrorAction SilentlyContinue) {
            try {
                $arguments = @('-c', 'import sys; print(sys.executable)')
                if ($command -eq 'py') { $arguments = @('-3') + $arguments }
                $candidates += (& $command @arguments 2>$null)
            } catch { }
        }
    }
    foreach ($candidate in $candidates) {
        if (Test-Python $candidate) { return $candidate }
    }
    return $null
}

function Install-Python {
    if (![Environment]::Is64BitOperatingSystem) { throw 'uwucraft requires 64-bit Windows 10/11.' }
    $home = Join-Path $target 'python'
    $executable = Join-Path $home 'python.exe'
    if (Test-Python $executable) { return $executable }
    $manifest = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'vendor\python-runtime.json') -Raw | ConvertFrom-Json
    $installer = Join-Path $PSScriptRoot 'vendor\python-installer.exe'
    $temporary = $null
    try {
        if (!(Test-Path $installer)) {
            $temporary = Join-Path ([IO.Path]::GetTempPath()) ('uwucraft-python-' + [guid]::NewGuid().ToString('N'))
            New-Item -ItemType Directory -Path $temporary | Out-Null
            $installer = Join-Path $temporary 'python-installer.exe'
            if ($manifest.url -notmatch '^https://www\.python\.org/ftp/python/') { throw 'Invalid Python source.' }
            [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
            Write-Host ('Downloading official Python ' + $manifest.version)
            Invoke-WebRequest -UseBasicParsing -Uri $manifest.url -OutFile $installer -TimeoutSec 300
        }
        if ((Get-FileHash -Algorithm SHA256 -LiteralPath $installer).Hash -ne $manifest.sha256) {
            throw 'Python installer SHA-256 mismatch. File rejected.'
        }
        $signature = Get-AuthenticodeSignature -LiteralPath $installer
        if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch '(?:CN|O)=Python Software Foundation(?:,|$)') {
            throw 'Python Software Foundation signature is not valid. File rejected.'
        }
        Write-Host ('Installing Python ' + $manifest.version + ' for uwucraft (current user).')
        $arguments = @('/quiet', 'InstallAllUsers=0', ('TargetDir="' + $home + '"'), 'Include_launcher=0', 'Include_test=0', 'Include_pip=1', 'PrependPath=0', 'Shortcuts=0', 'AssociateFiles=0')
        $process = Start-Process -FilePath $installer -ArgumentList $arguments -Wait -PassThru
        if ($process.ExitCode -notin @(0, 3010) -or !(Test-Python $executable)) {
            throw ('Python installation failed. Exit code: ' + $process.ExitCode)
        }
        return $executable
    } finally {
        if ($temporary) { Remove-Item -LiteralPath $temporary -Recurse -Force }
    }
}

$python = if ($BootstrapPython) { $null } else { Find-Python }
if (!$python) { $python = Install-Python }

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
