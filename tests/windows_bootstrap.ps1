[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'

function Assert-True {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) {
        throw $Message
    }
}

function Assert-BytesEqual {
    param([byte[]]$Expected, [byte[]]$Actual, [string]$Label)
    Assert-True ($Expected.Length -eq $Actual.Length) ("{0} changed length" -f $Label)
    for ($index = 0; $index -lt $Expected.Length; $index++) {
        Assert-True ($Expected[$index] -eq $Actual[$index]) ("{0} changed" -f $Label)
    }
}

function Invoke-Bootstrap {
    param([string[]]$Arguments)
    & $script:InstallScript @Arguments
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) {
        throw "install.ps1 failed with exit code $exitCode"
    }
}

function Invoke-CommandFile {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [string[]]$Arguments
    )
    $output = & $Path @Arguments 2>&1
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) {
        $detail = ($output | Out-String).Trim()
        throw ("Command file failed with exit code {0}: {1}`n{2}" -f $exitCode, $Path, $detail)
    }
    return ($output | Out-String).Trim()
}

$repo = (Get-Item -LiteralPath (Join-Path $PSScriptRoot '..')).FullName
$script:InstallScript = Join-Path $repo 'install.ps1'
$temporaryRoot = Join-Path ([System.IO.Path]::GetTempPath()) (
    'delegate-workers-windows-test-' + [guid]::NewGuid().ToString('N')
)
$temporaryRootCreated = $false
$scriptExitCode = 0

try {
    New-Item -ItemType Directory -Path $temporaryRoot | Out-Null
    $temporaryRootCreated = $true

    # Build the Unicode part at runtime so this file stays ASCII for PS 5.1.
    $codexHome = Join-Path $temporaryRoot ('codex home ' + [char]0x4e2d + [char]0x6587)
    New-Item -ItemType Directory -Path $codexHome -Force | Out-Null
    $sentinelPath = Join-Path $codexHome 'config.toml'
    $sentinelBytes = [System.Text.UTF8Encoding]::new($false).GetBytes(
        "model = 'kept-by-test'`nmodel_reasoning_effort = 'xhigh'`n"
    )
    [System.IO.File]::WriteAllBytes($sentinelPath, $sentinelBytes)

    $pathBefore = [Environment]::GetEnvironmentVariable('Path', 'Process')
    Invoke-Bootstrap @('-Source', $repo, '-CodexHome', $codexHome, '-NoPath')
    Assert-True ($pathBefore -eq [Environment]::GetEnvironmentVariable('Path', 'Process')) `
        'NoPath changed the current process PATH'

    $binDir = Join-Path $codexHome 'bin'
    $dw = Join-Path $binDir 'dw.cmd'
    $delegateWorkers = Join-Path $binDir 'delegate-workers.cmd'
    Assert-True (Test-Path -LiteralPath $dw -PathType Leaf) 'dw.cmd was not installed'
    Assert-True (Test-Path -LiteralPath $delegateWorkers -PathType Leaf) `
        'delegate-workers.cmd was not installed'

    $status = (Invoke-CommandFile -Path $dw -Arguments @('status')) | ConvertFrom-Json
    Assert-True ([bool]$status.installed) 'dw.cmd status did not report an installation'

    Invoke-CommandFile -Path $dw -Arguments @(
        'configure', '--profile', 'default', '--model', 'gpt-5.6-luna', '--effort', 'high'
    ) | Out-Null
    $settingsPath = Join-Path $codexHome 'skills\delegate-workers\workers.json'
    $settingsBeforeUpdate = [System.IO.File]::ReadAllBytes($settingsPath)

    Invoke-Bootstrap @(
        '-Action', 'update', '-Source', $repo, '-CodexHome', $codexHome, '-NoPath'
    )
    $settingsAfterUpdate = [System.IO.File]::ReadAllBytes($settingsPath)
    Assert-BytesEqual $settingsBeforeUpdate $settingsAfterUpdate 'workers.json'

    $statusAfterUpdate = (Invoke-CommandFile -Path $dw -Arguments @('status')) | ConvertFrom-Json
    Assert-True ([string]$statusAfterUpdate.config.profiles.default.reasoning_effort -eq 'high') `
        'update did not preserve the configured reasoning effort'

    Invoke-CommandFile -Path $dw -Arguments @('uninstall', '--yes') | Out-Null
    Assert-True (-not (Test-Path -LiteralPath $dw)) 'uninstall left dw.cmd behind'
    Assert-BytesEqual $sentinelBytes ([System.IO.File]::ReadAllBytes($sentinelPath)) 'config.toml'
}
catch {
    $scriptExitCode = 1
    [Console]::Error.WriteLine($_.Exception.Message)
}
finally {
    if ($temporaryRootCreated -and (Test-Path -LiteralPath $temporaryRoot)) {
        Remove-Item -LiteralPath $temporaryRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}

exit $scriptExitCode
