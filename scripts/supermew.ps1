param(
    [ValidateSet('start', 'stop', 'restart', 'status', 'logs')]
    [string]$Action = 'start',

    [switch]$NoDocker,
    [switch]$NoOllama,
    [switch]$NoBackend,
    [switch]$NoFrontend,
    [switch]$NoWait,
    [switch]$Foreground,

    [int]$BackendPort = 8050,
    [int]$FrontendPort = 3000
)

$ErrorActionPreference = 'Stop'

$ScriptPath = $PSCommandPath
if (-not $ScriptPath) {
    $ScriptPath = $MyInvocation.MyCommand.Path
}

$RepoRoot = Split-Path -Parent (Split-Path -Parent $ScriptPath)
$FrontendRoot = Join-Path $RepoRoot 'frontend'
$RuntimeRoot = Join-Path $RepoRoot 'tmp\ai-customer-service-dev'
$PidRoot = Join-Path $RuntimeRoot 'pids'
$LogRoot = Join-Path $RuntimeRoot 'logs'
$LegacyRuntimeName = 'super' + 'me' + 'w-dev'
$LegacyPidRoots = @(
    (Join-Path (Join-Path (Join-Path $RepoRoot 'tmp') $LegacyRuntimeName) 'pids')
)

function Ensure-Directory {
    param([Parameter(Mandatory = $true)][string]$Path)
    New-Item -ItemType Directory -Path $Path -Force | Out-Null
}

function Resolve-Executable {
    param([Parameter(Mandatory = $true)][string[]]$Names)

    foreach ($name in $Names) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if ($command) {
            return $command.Source
        }
    }

    return $null
}

function Test-PortOpen {
    param(
        [string]$HostName = '127.0.0.1',
        [Parameter(Mandatory = $true)][int]$Port
    )

    try {
        $client = [System.Net.Sockets.TcpClient]::new()
        $async = $client.BeginConnect($HostName, $Port, $null, $null)
        $connected = $async.AsyncWaitHandle.WaitOne(500)
        if ($connected) {
            $client.EndConnect($async)
        }
        $client.Close()
        return [bool]$connected
    } catch {
        return $false
    }
}

function Wait-Port {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][int]$Port,
        [int]$TimeoutSeconds = 90
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-PortOpen -Port $Port) {
            Write-Host "$Name is listening on http://127.0.0.1:$Port"
            return
        }
        Start-Sleep -Seconds 1
    }

    Write-Warning "$Name did not open port $Port within $TimeoutSeconds seconds. Check logs in $LogRoot."
}

function Get-PidFile {
    param([Parameter(Mandatory = $true)][string]$Name)
    return Join-Path $PidRoot "$Name.pid"
}

function Get-PidFileCandidates {
    param([Parameter(Mandatory = $true)][string]$Name)

    $pidFiles = @((Get-PidFile -Name $Name))
    foreach ($legacyPidRoot in $LegacyPidRoots) {
        $pidFiles += Join-Path $legacyPidRoot "$Name.pid"
    }

    return $pidFiles | Select-Object -Unique
}

function Get-LogFile {
    param([Parameter(Mandatory = $true)][string]$Name)
    return Join-Path $LogRoot "$Name.log"
}

function Ensure-RunnerScript {
    $runnerPath = Join-Path $RuntimeRoot 'run-managed.ps1'
    $runnerSource = @'
param(
    [Parameter(Mandatory = $true)][string]$Executable,
    [Parameter(Mandatory = $true)][string]$WorkingDirectory,
    [Parameter(Mandatory = $true)][string]$LogFile,
    [string]$CommandArgsJson = '[]'
)

$ErrorActionPreference = 'Stop'
$utf8 = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8
$env:NO_COLOR = '1'
$env:FORCE_COLOR = '0'

$parsedArgs = @()
if ($CommandArgsJson) {
    $parsed = $CommandArgsJson | ConvertFrom-Json
    $parsedArgs = @($parsed)
}

Set-Location -LiteralPath $WorkingDirectory
Add-Content -LiteralPath $LogFile -Value "[$(Get-Date -Format o)] Starting: $Executable $($parsedArgs -join ' ')"
& $Executable @parsedArgs *>> $LogFile
exit $LASTEXITCODE
'@
    Set-Content -LiteralPath $runnerPath -Value $runnerSource -Encoding ascii
    return $runnerPath
}

function Get-ManagedProcess {
    param([Parameter(Mandatory = $true)][string]$Name)

    foreach ($pidFile in (Get-PidFileCandidates -Name $Name)) {
        if (-not (Test-Path -LiteralPath $pidFile)) {
            continue
        }

        $rawPid = (Get-Content -LiteralPath $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
        $processId = 0
        if (-not [int]::TryParse($rawPid, [ref]$processId)) {
            continue
        }

        $process = Get-Process -Id $processId -ErrorAction SilentlyContinue
        if ($process) {
            return $process
        }
    }

    return $null
}

function Stop-ProcessTree {
    param([Parameter(Mandatory = $true)][int]$ProcessId)

    $children = Get-CimInstance Win32_Process -Filter "ParentProcessId = $ProcessId" -ErrorAction SilentlyContinue
    foreach ($child in $children) {
        Stop-ProcessTree -ProcessId ([int]$child.ProcessId)
    }

    $process = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if ($process) {
        Stop-Process -Id $ProcessId -Force
    }
}

function Stop-PortProcessIfOwned {
    param(
        [Parameter(Mandatory = $true)][int]$Port,
        [Parameter(Mandatory = $true)][string]$ExpectedPath
    )

    $listeners = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    foreach ($listener in $listeners) {
        $ownerPid = [int]$listener.OwningProcess
        if ($ownerPid -le 0) {
            continue
        }

        $owner = Get-CimInstance Win32_Process -Filter "ProcessId = $ownerPid" -ErrorAction SilentlyContinue
        if (-not $owner) {
            continue
        }

        $commandLine = [string]$owner.CommandLine
        if ($commandLine -and $commandLine.Contains($ExpectedPath)) {
            Write-Host "Stopping process on port $Port (PID $ownerPid)..."
            Stop-ProcessTree -ProcessId $ownerPid
        }
    }
}

function Stop-ManagedProcess {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [int]$Port = 0,
        [string]$ExpectedPath = $RepoRoot
    )

    foreach ($pidFile in (Get-PidFileCandidates -Name $Name)) {
        if (-not (Test-Path -LiteralPath $pidFile)) {
            continue
        }

        $rawPid = (Get-Content -LiteralPath $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
        $processId = 0
        if ([int]::TryParse($rawPid, [ref]$processId)) {
            $process = Get-Process -Id $processId -ErrorAction SilentlyContinue
            if ($process) {
                Write-Host "Stopping $Name (PID $($process.Id))..."
                Stop-ProcessTree -ProcessId $process.Id
                Start-Sleep -Milliseconds 500
            }
        }

        Remove-Item -LiteralPath $pidFile -Force
    }

    if ($Port -gt 0) {
        Stop-PortProcessIfOwned -Port $Port -ExpectedPath $ExpectedPath
    }
}

function Start-ManagedProcess {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$FileName,
        [Parameter(Mandatory = $true)][string[]]$ArgumentList,
        [Parameter(Mandatory = $true)][string]$WorkingDirectory
    )

    $existing = Get-ManagedProcess -Name $Name
    if ($existing) {
        Write-Host "$Name is already running (PID $($existing.Id))."
        return
    }

    $logFile = Get-LogFile -Name $Name
    $pidFile = Get-PidFile -Name $Name
    $runnerPath = Ensure-RunnerScript
    $pwsh = Resolve-Executable -Names @('pwsh', 'powershell')
    if (-not $pwsh) {
        throw 'PowerShell was not found on PATH.'
    }

    if (Test-Path -LiteralPath $logFile) {
        $timestamp = Get-Date -Format 'yyyyMMdd-HHmmss'
        Move-Item -LiteralPath $logFile -Destination "$logFile.$timestamp" -Force
    }

    $psi = [System.Diagnostics.ProcessStartInfo]::new()
    $psi.FileName = $pwsh
    $psi.WorkingDirectory = $WorkingDirectory
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = -not $Foreground

    $runnerArgs = @(
        '-NoLogo',
        '-NoProfile',
        '-NonInteractive',
        '-File',
        $runnerPath,
        '-Executable',
        $FileName,
        '-WorkingDirectory',
        $WorkingDirectory,
        '-LogFile',
        $logFile,
        '-CommandArgsJson',
        ($ArgumentList | ConvertTo-Json -Compress)
    )

    foreach ($arg in $runnerArgs) {
        $psi.ArgumentList.Add($arg)
    }

    $process = [System.Diagnostics.Process]::Start($psi)
    Set-Content -LiteralPath $pidFile -Value $process.Id -Encoding ascii

    Write-Host "Started $Name (PID $($process.Id)); log: $logFile"
}

function Invoke-DockerCompose {
    param([Parameter(Mandatory = $true)][string[]]$ComposeArgs)

    $docker = Resolve-Executable -Names @('docker')
    if (-not $docker) {
        throw 'docker was not found on PATH.'
    }

    Push-Location -LiteralPath $RepoRoot
    try {
        & $docker @ComposeArgs
        $exitCode = $LASTEXITCODE
        if ($exitCode -ne 0) {
            throw "docker failed with exit code $exitCode"
        }
    } finally {
        Pop-Location
    }
}

function Start-DockerServices {
    Write-Host 'Starting Docker services...'
    Invoke-DockerCompose -ComposeArgs @('compose', 'up', '-d')
}

function Stop-DockerServices {
    Write-Host 'Stopping Docker services...'
    Invoke-DockerCompose -ComposeArgs @('compose', 'stop')
}

function Start-Backend {
    $uv = Resolve-Executable -Names @('uv')
    if ($uv) {
        $fileName = $uv
        $arguments = @(
            'run',
            'uvicorn',
            'backend.app:app',
            '--host',
            '0.0.0.0',
            '--port',
            [string]$BackendPort,
            '--reload'
        )
    } else {
        $python = Join-Path $RepoRoot '.venv\Scripts\python.exe'
        if (-not (Test-Path -LiteralPath $python)) {
            $python = Resolve-Executable -Names @('python', 'py')
        }
        if (-not $python) {
            throw 'Neither uv nor python was found. Install uv or activate a Python environment first.'
        }
        $fileName = $python
        $arguments = @(
            '-m',
            'uvicorn',
            'backend.app:app',
            '--host',
            '0.0.0.0',
            '--port',
            [string]$BackendPort,
            '--reload'
        )
    }

    Start-ManagedProcess -Name 'backend' -FileName $fileName -ArgumentList $arguments -WorkingDirectory $RepoRoot
}

function Start-Ollama {
    # Ollama may be installed as a user-level application rather than being on PATH.
    # When another process already owns the configured API port, leave it untouched.
    if (Test-PortOpen -Port 11434) {
        Write-Host 'Ollama is already listening on http://127.0.0.1:11434.'
        return
    }

    $ollama = Resolve-Executable -Names @('ollama')
    if (-not $ollama) {
        throw 'Ollama was not found. Install Ollama, configure another OpenAI-compatible BASE_URL, or start with -NoOllama.'
    }

    Start-ManagedProcess -Name 'ollama' -FileName $ollama -ArgumentList @('serve') -WorkingDirectory $RepoRoot
}

function Start-Frontend {
    $npm = Resolve-Executable -Names @('npm.cmd', 'npm')
    if (-not $npm) {
        throw 'npm was not found on PATH.'
    }

    if (-not (Test-Path -LiteralPath (Join-Path $FrontendRoot 'node_modules'))) {
        Write-Host 'Installing frontend dependencies...'
        Push-Location -LiteralPath $FrontendRoot
        try {
            & $npm @('install')
            $exitCode = $LASTEXITCODE
            if ($exitCode -ne 0) {
                throw "npm install failed with exit code $exitCode"
            }
        } finally {
            Pop-Location
        }
    }

    Start-ManagedProcess -Name 'frontend' -FileName $npm -ArgumentList @('run', 'dev', '--', '--host', '0.0.0.0', '--port', [string]$FrontendPort) -WorkingDirectory $FrontendRoot
}

function Show-Status {
    $ollama = Get-ManagedProcess -Name 'ollama'
    $backend = Get-ManagedProcess -Name 'backend'
    $frontend = Get-ManagedProcess -Name 'frontend'

    $ollamaPortOpen = Test-PortOpen -Port 11434
    $ollamaStatus = if ($ollama) {
        "managed (PID $($ollama.Id))"
    } elseif ($ollamaPortOpen) {
        'running externally'
    } else {
        'stopped'
    }
    $backendStatus = if ($backend) { "running (PID $($backend.Id))" } else { 'stopped' }
    $frontendStatus = if ($frontend) { "running (PID $($frontend.Id))" } else { 'stopped' }
    $backendPortStatus = if (Test-PortOpen -Port $BackendPort) { 'open' } else { 'closed' }
    $frontendPortStatus = if (Test-PortOpen -Port $FrontendPort) { 'open' } else { 'closed' }

    Write-Host "Ollama:   $ollamaStatus; port 11434 $(if ($ollamaPortOpen) { 'open' } else { 'closed' })"
    Write-Host "Backend:  $backendStatus; port $BackendPort $backendPortStatus"
    Write-Host "Frontend: $frontendStatus; port $FrontendPort $frontendPortStatus"
    Write-Host "Logs:     $LogRoot"

    if (-not $NoDocker) {
        $docker = Resolve-Executable -Names @('docker')
        if ($docker) {
            Push-Location -LiteralPath $RepoRoot
            try {
                & $docker @('compose', 'ps')
            } finally {
                Pop-Location
            }
        }
    }
}

function Show-Logs {
    Write-Host "Ollama log:   $(Get-LogFile -Name 'ollama')"
    if (Test-Path -LiteralPath (Get-LogFile -Name 'ollama')) {
        Get-Content -LiteralPath (Get-LogFile -Name 'ollama') -Tail 80
    }

    Write-Host ''
    Write-Host "Backend log:  $(Get-LogFile -Name 'backend')"
    if (Test-Path -LiteralPath (Get-LogFile -Name 'backend')) {
        Get-Content -LiteralPath (Get-LogFile -Name 'backend') -Tail 80
    }

    Write-Host ''
    Write-Host "Frontend log: $(Get-LogFile -Name 'frontend')"
    if (Test-Path -LiteralPath (Get-LogFile -Name 'frontend')) {
        Get-Content -LiteralPath (Get-LogFile -Name 'frontend') -Tail 80
    }
}

Ensure-Directory -Path $RuntimeRoot
Ensure-Directory -Path $PidRoot
Ensure-Directory -Path $LogRoot

switch ($Action) {
    'start' {
        if (-not $NoDocker) {
            Start-DockerServices
        }
        if (-not $NoOllama) {
            Start-Ollama
        }
        if (-not $NoBackend) {
            Start-Backend
        }
        if (-not $NoFrontend) {
            Start-Frontend
        }
        if (-not $NoWait) {
            if (-not $NoOllama) {
                Wait-Port -Name 'Ollama' -Port 11434
            }
            if (-not $NoBackend) {
                Wait-Port -Name 'Backend' -Port $BackendPort
            }
            if (-not $NoFrontend) {
                Wait-Port -Name 'Frontend' -Port $FrontendPort
            }
        }
        Show-Status
        Write-Host ''
        Write-Host "Open frontend: http://127.0.0.1:$FrontendPort"
        Write-Host "Backend health: http://127.0.0.1:$BackendPort/health"
    }
    'stop' {
        if (-not $NoFrontend) {
            Stop-ManagedProcess -Name 'frontend' -Port $FrontendPort -ExpectedPath $FrontendRoot
        }
        if (-not $NoBackend) {
            Stop-ManagedProcess -Name 'backend' -Port $BackendPort -ExpectedPath $RepoRoot
        }
        if (-not $NoOllama) {
            # Port is deliberately omitted: never stop a globally managed Ollama instance.
            Stop-ManagedProcess -Name 'ollama'
        }
        if (-not $NoDocker) {
            Stop-DockerServices
        }
        Show-Status
    }
    'restart' {
        if (-not $NoFrontend) {
            Stop-ManagedProcess -Name 'frontend' -Port $FrontendPort -ExpectedPath $FrontendRoot
        }
        if (-not $NoBackend) {
            Stop-ManagedProcess -Name 'backend' -Port $BackendPort -ExpectedPath $RepoRoot
        }
        if (-not $NoOllama) {
            # Only stop an Ollama instance launched by this script.
            Stop-ManagedProcess -Name 'ollama'
        }
        if (-not $NoDocker) {
            Start-DockerServices
        }
        if (-not $NoOllama) {
            Start-Ollama
        }
        if (-not $NoBackend) {
            Start-Backend
        }
        if (-not $NoFrontend) {
            Start-Frontend
        }
        if (-not $NoWait) {
            if (-not $NoOllama) {
                Wait-Port -Name 'Ollama' -Port 11434
            }
            if (-not $NoBackend) {
                Wait-Port -Name 'Backend' -Port $BackendPort
            }
            if (-not $NoFrontend) {
                Wait-Port -Name 'Frontend' -Port $FrontendPort
            }
        }
        Show-Status
        Write-Host ''
        Write-Host "Open frontend: http://127.0.0.1:$FrontendPort"
        Write-Host "Backend health: http://127.0.0.1:$BackendPort/health"
    }
    'status' {
        Show-Status
    }
    'logs' {
        Show-Logs
    }
}
