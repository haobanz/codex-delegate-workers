[CmdletBinding()]
param(
    [ValidateSet('install', 'update', 'menu')]
    [string]$Action = 'install',
    [string]$Source,
    [string]$CodexHome,
    [string]$BinDir,
    [switch]$NoPath
)

$delegatePreviousErrorPreference = $ErrorActionPreference
$ErrorActionPreference = 'Stop'

function ConvertTo-WindowsArgument {
    param([AllowNull()][string]$Value)

    if ([string]::IsNullOrEmpty($Value)) {
        return '""'
    }
    if ($Value -notmatch '[\s"]') {
        return $Value
    }

    $escaped = [regex]::Replace($Value, '(\\*)"', '$1$1\"')
    $escaped = [regex]::Replace($escaped, '(\\+)$', '$1$1')
    return '"' + $escaped + '"'
}

function Invoke-Native {
    param(
        [Parameter(Mandatory = $true)]
        [string]$FilePath,
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments,
        [string]$OutputPath,
        [switch]$Interactive
    )

    if ($Interactive) {
        & $FilePath @Arguments
        $interactiveExitCode = $LASTEXITCODE
        if ($interactiveExitCode -ne 0) {
            $interactiveMessage = "Native command failed with exit code {0}: {1}" -f $interactiveExitCode, $FilePath
            $interactiveException = New-Object -TypeName System.Exception -ArgumentList $interactiveMessage
            $interactiveException.Data['ExitCode'] = [int]$interactiveExitCode
            throw $interactiveException
        }
        return
    }

    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $FilePath
    $startInfo.Arguments = (($Arguments | ForEach-Object {
                ConvertTo-WindowsArgument ([string]$_)
            }) -join ' ')
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $utf8 = [System.Text.UTF8Encoding]::new($false)
    $startInfo.StandardOutputEncoding = $utf8
    $startInfo.StandardErrorEncoding = $utf8

    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $startInfo
    try {
        $started = $process.Start()
        if (-not $started) {
            throw "Could not start native command: $FilePath"
        }
        $stdoutTask = $process.StandardOutput.ReadToEndAsync()
        $stderrTask = $process.StandardError.ReadToEndAsync()
        $process.WaitForExit()
        $stdout = $stdoutTask.Result
        $stderr = $stderrTask.Result
        $nativeExitCode = $process.ExitCode
    }
    finally {
        $process.Dispose()
    }

    if ($stderr) {
        [Console]::Error.Write($stderr)
    }

    if ($nativeExitCode -ne 0) {
        $exceptionMessage = "Native command failed with exit code {0}: {1}" -f $nativeExitCode, $FilePath
        $exception = New-Object -TypeName System.Exception -ArgumentList $exceptionMessage
        $exception.Data['ExitCode'] = [int]$nativeExitCode
        throw $exception
    }

    if ($OutputPath) {
        [System.IO.File]::WriteAllText($OutputPath, $stdout, $utf8)
    }
    elseif ($stdout) {
        Write-Output -NoEnumerate $stdout
    }
}

function Find-Python {
    $probe = 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'
    foreach ($candidateName in @('py', 'python')) {
        $candidate = Get-Command $candidateName -CommandType Application -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($null -eq $candidate) {
            continue
        }

        $candidatePath = $candidate.Path
        if ([string]::IsNullOrEmpty($candidatePath)) {
            $candidatePath = $candidate.Source
        }
        $prefix = @()
        if ($candidateName -eq 'py') {
            $prefix = @('-3')
        }
        $probeArguments = @($prefix) + @('-X', 'utf8', '-c', $probe)
        try {
            & $candidatePath @probeArguments > $null 2> $null
            $probeExitCode = $LASTEXITCODE
        }
        catch {
            $probeExitCode = 1
        }
        if ($probeExitCode -eq 0) {
            return [pscustomobject]@{
                Path = $candidatePath
                Prefix = $prefix
            }
        }
    }
    throw 'Python 3.10 or newer is required. Install Python and ensure py -3 or python is on PATH.'
}

function Find-Git {
    $candidate = Get-Command git -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($null -eq $candidate) {
        throw 'Git is required. Install Git and ensure git is on PATH.'
    }
    $candidatePath = $candidate.Path
    if ([string]::IsNullOrEmpty($candidatePath)) {
        $candidatePath = $candidate.Source
    }
    return $candidatePath
}

function New-BootstrapTempDirectory {
    $name = 'delegate-workers-bootstrap-' + [guid]::NewGuid().ToString('N')
    $path = Join-Path ([System.IO.Path]::GetTempPath()) $name
    New-Item -ItemType Directory -Path $path | Out-Null
    return $path
}

function Add-CurrentPath {
    param([Parameter(Mandatory = $true)][string]$Directory)

    $fullDirectory = [System.IO.Path]::GetFullPath($Directory)
    $currentPath = [Environment]::GetEnvironmentVariable('Path', 'Process')
    if ([string]::IsNullOrEmpty($currentPath)) {
        $env:Path = $fullDirectory
    }
    else {
        $env:Path = $fullDirectory + [System.IO.Path]::PathSeparator + $currentPath
    }
}

$bootstrapTemp = $null
$bootstrapTempCreated = $false
$scriptExitCode = 0
$delegateFailure = $null

try {
    $python = Find-Python
    $git = Find-Git
    $bootstrapTemp = New-BootstrapTempDirectory
    $bootstrapTempCreated = $true

    if ([string]::IsNullOrWhiteSpace($Source)) {
        $sourcePath = Join-Path $bootstrapTemp 'repository'
        Invoke-Native -FilePath $git -Arguments @(
            'clone', '--quiet', '--depth', '1', '--branch', 'main', '--',
            'https://github.com/haobanz/codex-delegate-workers.git', $sourcePath
        )
    }
    else {
        $sourceItem = Get-Item -LiteralPath $Source -ErrorAction Stop
        if (-not $sourceItem.PSIsContainer) {
            throw "Source is not a directory: $Source"
        }
        $sourcePath = $sourceItem.FullName
    }

    $managerPath = Join-Path $sourcePath 'skills\delegate-workers\scripts\manage.py'
    if (-not (Test-Path -LiteralPath $managerPath -PathType Leaf)) {
        throw "Source does not contain the delegate-workers manager: $managerPath"
    }

    $managerCommon = @()
    if (-not [string]::IsNullOrWhiteSpace($CodexHome)) {
        $managerCommon += @('--codex-home', $CodexHome)
    }
    if (-not [string]::IsNullOrWhiteSpace($BinDir)) {
        $managerCommon += @('--bin-dir', $BinDir)
    }
    $managerCommon += @('--source', $sourcePath)

    $managerArguments = @($python.Prefix) + @('-X', 'utf8', $managerPath) + $managerCommon + @($Action)
    if ($Action -eq 'menu') {
        Invoke-Native -FilePath $python.Path -Arguments $managerArguments -Interactive
    }
    else {
        Invoke-Native -FilePath $python.Path -Arguments $managerArguments
    }

    if (-not $NoPath) {
        $statusPath = Join-Path $bootstrapTemp 'status.json'
        $statusArguments = @($python.Prefix) + @('-X', 'utf8', $managerPath) + $managerCommon + @('status')
        Invoke-Native -FilePath $python.Path -Arguments $statusArguments -OutputPath $statusPath
        $statusText = [System.IO.File]::ReadAllText($statusPath, [System.Text.UTF8Encoding]::new($false))
        $status = $statusText | ConvertFrom-Json
        if ($status.installed -and $status.command_dir) {
            Add-CurrentPath -Directory ([string]$status.command_dir)
        }
    }
}
catch {
    $delegateFailure = $_.Exception
    $nativeCode = $_.Exception.Data['ExitCode']
    if ($null -ne $nativeCode) {
        $scriptExitCode = [int]$nativeCode
    }
    else {
        $scriptExitCode = 1
    }
    [Console]::Error.WriteLine($_.Exception.Message)
}
finally {
    if ($bootstrapTempCreated -and $bootstrapTemp -and (Test-Path -LiteralPath $bootstrapTemp)) {
        Remove-Item -LiteralPath $bootstrapTemp -Recurse -Force -ErrorAction SilentlyContinue
    }
    $ErrorActionPreference = $delegatePreviousErrorPreference
}

$global:LASTEXITCODE = $scriptExitCode
if ($null -ne $delegateFailure) {
    throw $delegateFailure
}
