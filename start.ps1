<#
  Vistora local production-like launcher.

  Usage:
    .\start.ps1
    .\start.ps1 -NoBrowser
    .\start.ps1 -NoInstall
    .\start.ps1 -BrowserCapture
    .\start.ps1 -BenchmarkAnalysis
    .\start.ps1 -FrontendOnly
    .\start.ps1 -WebPort 4180
    .\start.ps1 -NoDockerRepair

  The script starts the durable local stack: PostgreSQL, Redis, MinIO,
  migrations, Control API, Worker, and Web. Runtime files stay under var/.
#>

[CmdletBinding()]
param(
    [switch]$NoBrowser,
    [switch]$NoInstall,
    [switch]$BrowserCapture,
    [switch]$BenchmarkAnalysis,
    [switch]$FrontendOnly,
    [switch]$NoDockerRepair,
    [ValidateRange(1, 65535)]
    [int]$ApiPort = 8200,
    [ValidateRange(1, 65535)]
    [int]$WebPort = 4173,
    [string]$ProviderEnvFile = "",
    [ValidatePattern('^[a-z][a-z0-9_]{0,62}$')]
    [string]$DatabaseName = "vistora"
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$runtimeRoot = Join-Path $projectRoot "var\runtime"
$logRoot = Join-Path $projectRoot "var\logs"
$statePath = Join-Path $runtimeRoot "local-processes.json"
$composePath = Join-Path $projectRoot "deploy\docker-compose.persistence.yml"
$venvRoot = Join-Path $projectRoot ".venv"
$venvPython = Join-Path $venvRoot "Scripts\python.exe"
$webRoot = Join-Path $projectRoot "apps\web"
$apiUrl = "http://127.0.0.1:$ApiPort"
$webUrl = "http://127.0.0.1:$WebPort"
$databaseName = $DatabaseName
$databaseUser = "vistora"
$databasePassword = @("vistora", "local", "only") -join "-"
$postgresPort = 55433
$redisPort = 56380
$minioApiPort = 59002
$minioConsolePort = 59003
$browserProxyPort = 58888
$redisNamespace = "vistora-local"
$s3Bucket = "vistora-local"
$s3AccessKey = "vistora"
$s3SecretKey = "vistora-local-only"
if ([string]::IsNullOrWhiteSpace($ProviderEnvFile)) {
    $ProviderEnvFile = Join-Path $projectRoot "var\secrets\worker-provider.env"
}
$providerEnvironmentKeys = @(
    "FRAMEFACTORY_OPENAI_BASE_URL",
    "FRAMEFACTORY_OPENAI_API_KEY",
    "FRAMEFACTORY_OPENAI_RESEARCH_MODEL",
    "FRAMEFACTORY_OPENAI_WRITING_MODEL",
    "FRAMEFACTORY_OPENAI_QUALITY_MODEL",
    "FRAMEFACTORY_OPENAI_TIMEOUT_SECONDS",
    "FRAMEFACTORY_RESEARCH_SEARCH_URL",
    "FRAMEFACTORY_RESEARCH_SEARCH_BEARER_TOKEN",
    "FRAMEFACTORY_RESEARCH_SEARCH_BEARER_TOKEN_SOURCE",
    "FRAMEFACTORY_RESEARCH_SEARCH_PROTOCOL",
    "FRAMEFACTORY_RESEARCH_SEARCH_MODEL",
    "FRAMEFACTORY_RESEARCH_SEARCH_TIMEOUT_SECONDS",
    "FRAMEFACTORY_RESEARCH_SEARCH_MAX_RESPONSE_BYTES",
    "FRAMEFACTORY_ASSET_VISION_BASE_URL",
    "FRAMEFACTORY_ASSET_VISION_API_KEY",
    "FRAMEFACTORY_ASSET_VISION_MODEL",
    "FRAMEFACTORY_ASSET_VISION_TIMEOUT_SECONDS",
    "FRAMEFACTORY_RUNWAY_BASE_URL",
    "FRAMEFACTORY_RUNWAY_API_KEY",
    "FRAMEFACTORY_RUNWAY_MODEL",
    "FRAMEFACTORY_RUNWAY_TIMEOUT_SECONDS",
    "FRAMEFACTORY_WAN_BASE_URL",
    "FRAMEFACTORY_WAN_API_KEY",
    "FRAMEFACTORY_WAN_API_KEY_SOURCE",
    "FRAMEFACTORY_WAN_MODEL",
    "FRAMEFACTORY_WAN_COST_PER_SECOND_MINOR",
    "FRAMEFACTORY_WAN_TIMEOUT_SECONDS",
    "FRAMEFACTORY_FULL_AI_VISION_BASE_URL",
    "FRAMEFACTORY_FULL_AI_VISION_API_KEY",
    "FRAMEFACTORY_FULL_AI_VISION_API_KEY_SOURCE",
    "FRAMEFACTORY_FULL_AI_VISION_MODEL",
    "FRAMEFACTORY_FULL_AI_VISION_TIMEOUT_SECONDS",
    "FRAMEFACTORY_FULL_AI_PROVIDER_NAME",
    "FRAMEFACTORY_FULL_AI_MODEL_ID",
    "FRAMEFACTORY_FULL_AI_COST_PER_SECOND_MINOR",
    "FRAMEFACTORY_FULL_AI_CREDIT_UNIT_MINOR",
    "FRAMEFACTORY_FULL_AI_TERMS_REFERENCE",
    "FRAMEFACTORY_FULL_AI_TERMS_CONTENT_HASH",
    "FRAMEFACTORY_FULL_AI_TERMS_CAPTURED_AT",
    "FRAMEFACTORY_FULL_AI_PRICING_REFERENCE",
    "FRAMEFACTORY_FULL_AI_PRICING_CONTENT_HASH",
    "FRAMEFACTORY_FULL_AI_PRICING_CAPTURED_AT",
    "FRAMEFACTORY_FULL_AI_OUTPUT_RIGHTS_CONFIRMED",
    "FRAMEFACTORY_FULL_AI_OUTPUT_RIGHTS_LICENSE_BASIS",
    "FRAMEFACTORY_ASR_BASE_URL",
    "FRAMEFACTORY_ASR_API_KEY",
    "FRAMEFACTORY_ASR_MODEL",
    "FRAMEFACTORY_ASR_TIMEOUT_SECONDS",
    "FRAMEFACTORY_ASR_RESPONSE_FORMAT",
    "FRAMEFACTORY_ASR_TIMESTAMP_MODE",
    "FRAMEFACTORY_XHS_MANAGED_BROWSER_BASE_URL"
)

function Write-Info { param([string]$Message) Write-Host "[i] $Message" -ForegroundColor Cyan }
function Write-Good { param([string]$Message) Write-Host "[OK] $Message" -ForegroundColor Green }
function Stop-WithError {
    param([string]$Message)
    Write-Host "[X] $Message" -ForegroundColor Red
    exit 1
}

function Require-Command {
    param([string]$Name, [string]$InstallHint)
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        Stop-WithError "找不到 $Name。$InstallHint"
    }
}

function Invoke-Checked {
    param(
        [string]$FilePath,
        [string[]]$Arguments,
        [string]$FailureMessage,
        [int]$TimeoutSeconds = 0,
        [switch]$CaptureOutput
    )
    if ($TimeoutSeconds -gt 0) {
        $command = Get-Command $FilePath -ErrorAction Stop
        $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
        $startInfo.FileName = $command.Source
        $startInfo.WorkingDirectory = $projectRoot
        $startInfo.UseShellExecute = $false
        $startInfo.CreateNoWindow = $true
        $startInfo.RedirectStandardOutput = $true
        $startInfo.RedirectStandardError = $true
        foreach ($argument in $Arguments) {
            [void]$startInfo.ArgumentList.Add($argument)
        }

        $process = [System.Diagnostics.Process]::new()
        $process.StartInfo = $startInfo
        try {
            [void]$process.Start()
            $stdoutTask = $process.StandardOutput.ReadToEndAsync()
            $stderrTask = $process.StandardError.ReadToEndAsync()
            if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
                $process.Kill($true)
                $process.WaitForExit()
                throw "$FailureMessage（超过 $TimeoutSeconds 秒，已终止命令）"
            }
            $stdout = $stdoutTask.GetAwaiter().GetResult()
            $stderr = $stderrTask.GetAwaiter().GetResult()
            if ($process.ExitCode -ne 0) {
                if (-not [string]::IsNullOrWhiteSpace($stderr)) {
                    Write-Host $stderr.TrimEnd() -ForegroundColor DarkRed
                }
                throw "$FailureMessage（退出码 $($process.ExitCode)）"
            }
            if ($CaptureOutput) {
                return $stdout
            }
            if (-not [string]::IsNullOrWhiteSpace($stdout)) {
                Write-Host $stdout.TrimEnd()
            }
            if (-not [string]::IsNullOrWhiteSpace($stderr)) {
                Write-Host $stderr.TrimEnd() -ForegroundColor DarkYellow
            }
            return
        } finally {
            $process.Dispose()
        }
    }

    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$FailureMessage（退出码 $LASTEXITCODE）"
    }
}

function Get-VirtualEnvironmentStatus {
    param(
        [string]$Root,
        [string]$PythonPath,
        [string]$RequiredVersion
    )
    if (-not (Test-Path -LiteralPath $Root)) {
        return [pscustomobject]@{
            Exists = $false
            Valid = $false
            Reason = ".venv 不存在"
        }
    }
    if (-not (Test-Path -LiteralPath $Root -PathType Container)) {
        return [pscustomobject]@{
            Exists = $true
            Valid = $false
            Reason = ".venv 存在但不是目录"
        }
    }
    if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
        return [pscustomobject]@{
            Exists = $true
            Valid = $false
            Reason = ".venv 缺少 Scripts\python.exe"
        }
    }
    try {
        $version = (& $PythonPath -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null | Out-String).Trim()
        $exitCode = $LASTEXITCODE
    } catch {
        return [pscustomobject]@{
            Exists = $true
            Valid = $false
            Reason = ".venv 的 Python 无法执行：$($_.Exception.Message)"
        }
    }
    if ($exitCode -ne 0 -or [string]::IsNullOrWhiteSpace($version)) {
        return [pscustomobject]@{
            Exists = $true
            Valid = $false
            Reason = ".venv 的 Python 无法执行（退出码 $exitCode）"
        }
    }
    if ($version -ne $RequiredVersion) {
        return [pscustomobject]@{
            Exists = $true
            Valid = $false
            Reason = ".venv 需要 Python $RequiredVersion，当前为 $version"
        }
    }
    return [pscustomobject]@{
        Exists = $true
        Valid = $true
        Reason = ""
    }
}

function Move-InvalidVirtualEnvironmentToBackup {
    param([string]$Root)
    $parent = Split-Path -Parent $Root
    $leaf = Split-Path -Leaf $Root
    $stamp = [DateTime]::UtcNow.ToString("yyyyMMdd-HHmmss")
    $backup = Join-Path $parent "$leaf.invalid-$stamp"
    $suffix = 1
    while (Test-Path -LiteralPath $backup) {
        $backup = Join-Path $parent "$leaf.invalid-$stamp-$suffix"
        $suffix += 1
    }
    Move-Item -LiteralPath $Root -Destination $backup
    return $backup
}

function Test-Http {
    param([string]$Url)
    try {
        $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 2
        return $response.StatusCode -ge 200 -and $response.StatusCode -lt 400
    } catch {
        return $false
    }
}

function Import-ProviderEnvironment {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        Write-Info "未发现新版 Provider 配置；文本类能力将保持关闭"
        return
    }
    $allowed = $providerEnvironmentKeys
    $seen = @{}
    foreach ($rawLine in Get-Content -LiteralPath $Path) {
        $line = $rawLine.Trim()
        if (-not $line -or $line.StartsWith("#")) { continue }
        $parts = $line.Split("=", 2)
        if ($parts.Count -ne 2) { Stop-WithError "Provider 配置包含无效行" }
        $name = $parts[0].Trim()
        $value = $parts[1].Trim()
        if ($allowed -notcontains $name) { Stop-WithError "Provider 配置包含不允许的键：$name" }
        if ($seen.ContainsKey($name)) { Stop-WithError "Provider 配置包含重复键：$name" }
        if ([string]::IsNullOrWhiteSpace($value)) { Stop-WithError "Provider 配置值不能为空：$name" }
        Set-Item -LiteralPath "Env:$name" -Value $value
        $seen[$name] = $true
    }
    Write-Good "已从新版私密目录载入 Provider 配置"
}

function Clear-ProviderEnvironment {
    foreach ($name in $providerEnvironmentKeys) {
        Remove-Item -LiteralPath "Env:$name" -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath "Env:${name}_FILE" -ErrorAction SilentlyContinue
    }
    Remove-Item Env:FRAMEFACTORY_LEGACY_MEDIA_ENABLED -ErrorAction SilentlyContinue
    Remove-Item Env:FRAMEFACTORY_LEGACY_TTS_VOICE -ErrorAction SilentlyContinue
    Remove-Item Env:FRAMEFACTORY_LEGACY_TTS_RATE -ErrorAction SilentlyContinue
}

function Test-DurableApi {
    try {
        $response = Invoke-RestMethod -Uri "$apiUrl/healthz" -TimeoutSec 2
        return $response.status -eq "ok" -and $response.persistence -eq "postgresql"
    } catch {
        return $false
    }
}

function Wait-Http {
    param(
        [string]$Url,
        [string]$Name,
        [int]$Attempts = 60,
        [int]$ProcessId = 0
    )
    for ($attempt = 0; $attempt -lt $Attempts; $attempt++) {
        if (Test-Http $Url) {
            Write-Good "$Name 已就绪"
            return
        }
        if ($ProcessId -gt 0 -and -not (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)) {
            throw "$Name 进程 PID $ProcessId 在就绪前退出，请查看 $logRoot"
        }
        Start-Sleep -Milliseconds 500
    }
    throw "$Name 启动超时，请查看 $logRoot"
}

function Get-ListeningProcessId {
    param([int]$Port)
    $connection = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($connection) { return [int]$connection.OwningProcess }
    foreach ($line in @(& netstat.exe -ano -p tcp 2>$null)) {
        if ($line -match "^\s*TCP\s+\S+:${Port}\s+\S+\s+LISTENING\s+(\d+)\s*$") {
            return [int]$Matches[1]
        }
    }
    return $null
}

function Assert-PortAvailableOrHealthy {
    param([int]$Port, [string]$HealthUrl, [string]$Name, [switch]$RequireDurableApi)
    $owner = Get-ListeningProcessId -Port $Port
    if ($null -eq $owner) { return $false }
    $healthy = if ($RequireDurableApi) { Test-DurableApi } else { Test-Http $HealthUrl }
    if ($healthy) {
        Write-Good "$Name 已在端口 $Port 运行，本次复用"
        return $true
    }
    Stop-WithError "端口 $Port 已由 PID $owner 占用，但不是可用的 $Name"
}

function Start-ManagedProcess {
    param(
        [string]$Name,
        [string]$FilePath,
        [string[]]$Arguments,
        [string]$WorkingDirectory
    )
    $stdout = Join-Path $logRoot "$Name.stdout.log"
    $stderr = Join-Path $logRoot "$Name.stderr.log"
    $archiveRoot = Join-Path $logRoot "archive"
    $archiveStamp = Get-Date -Format "yyyyMMdd-HHmmss-fff"
    foreach ($log in @($stdout, $stderr)) {
        if (Test-Path -LiteralPath $log) {
            New-Item -ItemType Directory -Force -Path $archiveRoot | Out-Null
            $leaf = [System.IO.Path]::GetFileNameWithoutExtension($log)
            $extension = [System.IO.Path]::GetExtension($log)
            Move-Item -LiteralPath $log -Destination (Join-Path $archiveRoot "$leaf.$archiveStamp$extension")
        }
    }
    $process = Start-Process -FilePath $FilePath -ArgumentList $Arguments `
        -WorkingDirectory $WorkingDirectory -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $stdout -RedirectStandardError $stderr
    $tracked = Get-Process -Id $process.Id -ErrorAction Stop
    return [ordered]@{
        name = $Name
        pid = $process.Id
        started_at_utc = $tracked.StartTime.ToString("o")
        started_at_filetime_utc = $tracked.StartTime.ToFileTimeUtc()
    }
}

New-Item -ItemType Directory -Force -Path $runtimeRoot, $logRoot | Out-Null

if (Test-Path -LiteralPath $statePath) {
    $previous = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
    $live = @($previous.processes | Where-Object {
        $candidate = Get-Process -Id ([int]$_.pid) -ErrorAction SilentlyContinue
        if (-not $candidate) { return $false }
        if ($null -ne $_.started_at_filetime_utc) {
            return [Math]::Abs(
                $candidate.StartTime.ToFileTimeUtc() - [long]$_.started_at_filetime_utc
            ) -le [TimeSpan]::TicksPerSecond * 2
        }
        $actual = $candidate.StartTime
        $recorded = [DateTime]::Parse([string]$_.started_at_utc)
        return [Math]::Abs(($actual - $recorded).TotalSeconds) -le 2
    })
    if ($live.Count -gt 0) {
        Stop-WithError "已有 start.ps1 管理的进程仍在运行，请先执行 .\stop.ps1"
    }
    Remove-Item -LiteralPath $statePath
}

# Vinext allows only one development server per app directory. Detect a manual
# or orphaned server before starting infrastructure so the launcher fails fast
# instead of waiting for the Web readiness timeout after API/Worker startup.
try {
    $conflictingWebProcesses = @(Get-CimInstance Win32_Process `
        -Filter "Name = 'node.exe'" -ErrorAction Stop | Where-Object {
            $commandLine = [string]$_.CommandLine
            $commandLine.IndexOf($webRoot, [StringComparison]::OrdinalIgnoreCase) -ge 0 -and
                $commandLine.IndexOf("vinext", [StringComparison]::OrdinalIgnoreCase) -ge 0 -and
                $commandLine -match "\bdev\b"
        })
    if ($conflictingWebProcesses.Count -gt 0) {
        $conflictingPids = ($conflictingWebProcesses.ProcessId -join ", ")
        Stop-WithError "检测到本项目已有 vinext dev 进程（PID: $conflictingPids）；请先停止它或执行 .\stop.ps1"
    }
} catch {
    Write-Info "无法预检既有 vinext dev 进程，将继续并依赖 Web 就绪检查：$($_.Exception.Message)"
}

Require-Command -Name "node" -InstallHint "请安装 Node.js 22.13 或更高版本。"
Require-Command -Name "npm" -InstallHint "npm 应随 Node.js 一起安装。"

$nodeMajor = [int]((& node -p "process.versions.node.split('.')[0]").Trim())
if ($nodeMajor -lt 22) {
    Stop-WithError "需要 Node.js 22 或更高版本，当前为 $(& node -v)"
}

function Wait-Tcp {
    param([string]$Address, [int]$Port, [string]$Name, [int]$Attempts = 60)
    for ($attempt = 0; $attempt -lt $Attempts; $attempt++) {
        $client = [System.Net.Sockets.TcpClient]::new()
        try {
            $client.Connect($Address, $Port)
            Write-Good "$Name 已就绪"
            return
        } catch {
            Start-Sleep -Milliseconds 500
        } finally {
            $client.Dispose()
        }
    }
    throw "$Name 启动超时，请查看 $logRoot"
}

function Clear-BrowserCaptureEnvironment {
    $names = @(
        "FRAMEFACTORY_STEP_QUEUE",
        "FRAMEFACTORY_WORKER_ID",
        "FRAMEFACTORY_WORKER_CONCURRENCY",
        "FRAMEFACTORY_BROWSER_CAPTURE_ONLY",
        "FRAMEFACTORY_BROWSER_CAPTURE_ENABLED",
        "FRAMEFACTORY_BROWSER_EGRESS_POLICY_ENFORCED",
        "FRAMEFACTORY_BROWSER_EGRESS_PROXY_URL",
        "FRAMEFACTORY_BROWSER_ALLOW_INSECURE_LOOPBACK_PROXY",
        "FRAMEFACTORY_BROWSER_DISABLE_CHROMIUM_SANDBOX",
        "FRAMEFACTORY_BROWSER_ALLOW_NON_STANDARD_PORTS",
        "FRAMEFACTORY_S3_KEY_PREFIX"
    )
    foreach ($name in $names) {
        Remove-Item -LiteralPath "Env:$name" -ErrorAction SilentlyContinue
    }
}

function Get-DescendantProcessIds {
    param([int]$RootProcessId)
    try {
        $all = @(Get-CimInstance Win32_Process -ErrorAction Stop)
    } catch {
        return @()
    }
    $result = [System.Collections.Generic.List[int]]::new()
    $frontier = [System.Collections.Generic.Queue[int]]::new()
    $frontier.Enqueue($RootProcessId)
    while ($frontier.Count -gt 0) {
        $parent = $frontier.Dequeue()
        foreach ($child in $all | Where-Object { [int]$_.ParentProcessId -eq $parent }) {
            $childId = [int]$child.ProcessId
            $result.Add($childId)
            $frontier.Enqueue($childId)
        }
    }
    return @($result)
}

function Stop-ManagedProcesses {
    param([object[]]$Processes)
    $records = @($Processes)
    [array]::Reverse($records)
    foreach ($entry in $records) {
        $process = Get-Process -Id ([int]$entry.pid) -ErrorAction SilentlyContinue
        if (-not $process) { continue }
        $sameProcess = [Math]::Abs(
            $process.StartTime.ToFileTimeUtc() - [long]$entry.started_at_filetime_utc
        ) -le [TimeSpan]::TicksPerSecond * 2
        if (-not $sameProcess) { continue }
        if (Get-Command "taskkill.exe" -ErrorAction SilentlyContinue) {
            & taskkill.exe /PID $process.Id /T /F 2>$null | Out-Null
            if ($LASTEXITCODE -eq 0) { continue }
        }
        $descendants = @(Get-DescendantProcessIds -RootProcessId $process.Id)
        if ($descendants.Count -gt 0) {
            Stop-Process -Id ($descendants | Sort-Object -Descending) -Force -ErrorAction SilentlyContinue
        }
        Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        Start-Sleep -Milliseconds 100
        if (Get-Process -Id $process.Id -ErrorAction SilentlyContinue) {
            throw "无法终止受管进程 $($entry.name) PID $($entry.pid)；状态文件已保留，可重试 .\stop.ps1"
        }
    }
}

function Save-ManagedProcessState {
    param([object[]]$Processes)
    $state = [ordered]@{
        project_root = $projectRoot
        created_at_utc = [DateTime]::UtcNow.ToString("o")
        processes = @($Processes)
    }
    $state | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $statePath -Encoding UTF8
}

function Move-StaleDockerSocketDirectory {
    param(
        [string]$Path,
        [string]$ExpectedParent
    )
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        return $null
    }

    $resolvedPath = [IO.Path]::GetFullPath($Path).TrimEnd("\")
    $resolvedParent = [IO.Path]::GetFullPath($ExpectedParent).TrimEnd("\")
    if (-not [string]::Equals(
        [IO.Path]::GetDirectoryName($resolvedPath),
        $resolvedParent,
        [StringComparison]::OrdinalIgnoreCase
    )) {
        throw "拒绝移动非预期 Docker socket 目录：$resolvedPath"
    }

    $entries = @(Get-ChildItem -Force -LiteralPath $resolvedPath -ErrorAction Stop)
    $unsafeEntries = @($entries | Where-Object {
        $_.PSIsContainer -or
            -not ($_.Attributes -band [IO.FileAttributes]::ReparsePoint)
    })
    if ($unsafeEntries.Count -gt 0) {
        $names = ($unsafeEntries.Name -join ", ")
        throw "Docker socket 目录包含非临时内容，拒绝自动修复：$resolvedPath（$names）"
    }

    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $destination = "$resolvedPath.stale-vistora-$stamp"
    $suffix = 0
    while (Test-Path -LiteralPath $destination) {
        $suffix += 1
        $destination = "$resolvedPath.stale-vistora-$stamp-$suffix"
    }
    Move-Item -LiteralPath $resolvedPath -Destination $destination
    return $destination
}

function Repair-DockerDesktopStartup {
    param([string]$OriginalFailure)
    if ($NoDockerRepair) {
        throw $OriginalFailure
    }
    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
        throw $OriginalFailure
    }

    $dockerProcesses = @(Get-Process -Name @(
        "Docker Desktop",
        "com.docker.backend",
        "com.docker.build",
        "com.docker.proxy"
    ) -ErrorAction SilentlyContinue)
    if ($dockerProcesses.Count -gt 0) {
        throw "$OriginalFailure。Docker Desktop 进程仍在运行，退出 Docker Desktop 后重试"
    }

    $localAppData = [Environment]::GetFolderPath(
        [Environment+SpecialFolder]::LocalApplicationData
    )
    if ([string]::IsNullOrWhiteSpace($localAppData)) {
        throw "$OriginalFailure。无法解析 LocalApplicationData"
    }
    $dockerParent = Join-Path $localAppData "Docker"
    $socketPaths = @(
        [ordered]@{ Path = Join-Path $dockerParent "run"; Parent = $dockerParent },
        [ordered]@{ Path = Join-Path $localAppData "docker-secrets-engine"; Parent = $localAppData }
    )
    $backups = @()
    foreach ($entry in $socketPaths) {
        $backup = Move-StaleDockerSocketDirectory -Path $entry.Path `
            -ExpectedParent $entry.Parent
        if ($backup) {
            $backups += $backup
            Write-Info "已保留异常 Docker socket 目录：$backup"
        }
    }

    Write-Info "尝试启动 Docker Desktop"
    Invoke-Checked -FilePath "docker" -Arguments @("desktop", "start") `
        -FailureMessage "Docker Desktop 自动启动失败" -TimeoutSeconds 90
    for ($attempt = 0; $attempt -lt 12; $attempt++) {
        try {
            $serverVersion = ([string](Invoke-Checked -FilePath "docker" -Arguments @(
                "info", "--format", "{{.ServerVersion}}"
            ) -FailureMessage "Docker Engine 尚未就绪" -TimeoutSeconds 10 -CaptureOutput)).Trim()
            if (-not [string]::IsNullOrWhiteSpace($serverVersion)) {
                Write-Good "Docker Desktop 已恢复：$serverVersion"
                return
            }
        } catch {
            if ($attempt -eq 11) {
                $backupHint = if ($backups.Count -gt 0) {
                    "；socket 备份：$($backups -join ', ')"
                } else { "" }
                throw "Docker Desktop 自动修复后仍未就绪$backupHint"
            }
            Start-Sleep -Seconds 2
        }
    }
}

function Select-LocalRedisImage {
    if (-not [string]::IsNullOrWhiteSpace($env:FRAMEFACTORY_REDIS_IMAGE)) {
        return
    }

    $officialRedisImage = ([string](Invoke-Checked -FilePath "docker" -Arguments @(
        "image", "ls", "--quiet", "--filter", "reference=redis:7.4-alpine"
    ) -FailureMessage "检查 Redis 官方镜像失败" -TimeoutSeconds 20 -CaptureOutput)).Trim()
    $localRedisImage = ([string](Invoke-Checked -FilePath "docker" -Arguments @(
        "image", "ls", "--quiet", "--filter", "reference=framefactory/redis-local:7.2.9-r0"
    ) -FailureMessage "检查 Redis 本地镜像失败" -TimeoutSeconds 20 -CaptureOutput)).Trim()
    if (-not $officialRedisImage -and $localRedisImage) {
        $env:FRAMEFACTORY_REDIS_IMAGE = "framefactory/redis-local:7.2.9-r0"
        Write-Info "使用已验证的本机 Redis 开发镜像"
    }
}

function Assert-FullAiApiConfiguration {
    param(
        [bool]$ExpectedReady,
        [string]$ExpectedProvider,
        [string]$ExpectedModel
    )
    try {
        $options = Invoke-RestMethod -Uri "$apiUrl/v1/full-ai/options" -TimeoutSec 5
    } catch {
        throw "无法核对 Control API 的 Full-AI 配置；请停止旧服务后重试"
    }
    $actualReady = $options.status -eq "ready"
    if ($actualReady -ne $ExpectedReady) {
        throw "Control API 的 Full-AI readiness 与本次配置不一致；请先执行 .\stop.ps1"
    }
    if ($ExpectedReady -and (
        $options.provider.name -ne $ExpectedProvider -or
        $options.provider.model_id -ne $ExpectedModel
    )) {
        throw "Control API 的 Full-AI Provider 指纹与本次配置不一致；请先执行 .\stop.ps1"
    }
}

function Suspend-WorkerProviderSecrets {
    $saved = @{}
    $secretNames = @(
        "FRAMEFACTORY_OPENAI_API_KEY",
        "FRAMEFACTORY_OPENAI_API_KEY_FILE",
        "FRAMEFACTORY_RESEARCH_SEARCH_BEARER_TOKEN",
        "FRAMEFACTORY_RESEARCH_SEARCH_BEARER_TOKEN_FILE",
        "FRAMEFACTORY_ASSET_VISION_API_KEY",
        "FRAMEFACTORY_ASSET_VISION_API_KEY_FILE",
        "FRAMEFACTORY_ASR_API_KEY",
        "FRAMEFACTORY_ASR_API_KEY_FILE",
        "FRAMEFACTORY_RUNWAY_API_KEY",
        "FRAMEFACTORY_RUNWAY_API_KEY_FILE",
        "FRAMEFACTORY_WAN_API_KEY",
        "FRAMEFACTORY_WAN_API_KEY_FILE",
        "FRAMEFACTORY_FULL_AI_VISION_API_KEY",
        "FRAMEFACTORY_FULL_AI_VISION_API_KEY_FILE"
    )
    foreach ($name in $secretNames) {
        $value = [Environment]::GetEnvironmentVariable($name)
        if ($null -ne $value) {
            $saved[$name] = $value
            Remove-Item -LiteralPath "Env:$name" -ErrorAction SilentlyContinue
        }
    }
    return $saved
}

function Restore-WorkerProviderSecrets {
    param([hashtable]$Secrets)
    foreach ($name in $Secrets.Keys) {
        Set-Item -LiteralPath "Env:$name" -Value $Secrets[$name]
    }
}

if ($FrontendOnly) {
    if ($BrowserCapture) {
        Stop-WithError "-FrontendOnly 不能与 -BrowserCapture 同时使用"
    }
    if (-not $NoInstall) {
        Write-Info "按锁文件校验 Web 依赖"
        Push-Location $webRoot
        try {
            Invoke-Checked -FilePath "npm" -Arguments @("ci") -FailureMessage "Web 依赖安装失败"
        } finally {
            Pop-Location
        }
    }

    $env:NEXT_PUBLIC_FRAMEFACTORY_API_URL = $apiUrl

    $managed = @()
    $webProcessRecord = $null
    try {
        $webReused = Assert-PortAvailableOrHealthy -Port $WebPort -HealthUrl $webUrl -Name "Web"
        if (-not $webReused) {
            Write-Info "以仅前端模式启动 Web"
            $webProcessRecord = Start-ManagedProcess -Name "web" -FilePath "npm.cmd" -Arguments @(
                "run", "dev", "--", "--hostname", "127.0.0.1", "--port", "$WebPort"
            ) -WorkingDirectory $webRoot
            $managed += $webProcessRecord
            Save-ManagedProcessState -Processes $managed
        }
        if ($webProcessRecord) {
            Wait-Http -Url $webUrl -Name "Web" -ProcessId $webProcessRecord.pid
        } else {
            Wait-Http -Url $webUrl -Name "Web"
        }
        Save-ManagedProcessState -Processes $managed
    } catch {
        Stop-ManagedProcesses -Processes $managed
        Remove-Item -LiteralPath $statePath -Force -ErrorAction SilentlyContinue
        Stop-WithError $_.Exception.Message
    }

    Write-Good "Vistora 前端已启动：$webUrl/create"
    Write-Host "注意：当前为仅前端模式，依赖 Control API 的功能会显示连接失败。"
    Write-Host "日志：$logRoot"
    Write-Host "停止：.\stop.ps1"
    if (-not $NoBrowser) {
        Start-Process "$webUrl/create"
    }
    return
}

Require-Command -Name "docker" -InstallHint "请安装并启动 Docker Desktop。"
$dockerContext = ([string](Invoke-Checked -FilePath "docker" -Arguments @(
    "context", "show"
) -FailureMessage "无法读取 Docker context" -TimeoutSeconds 20 -CaptureOutput)).Trim()
if ($dockerContext -ne "desktop-linux") {
    Stop-WithError "当前 Docker context 为 '$dockerContext'；为避免误操作远程资源，请切换到 desktop-linux"
}

try {
    Invoke-Checked -FilePath "docker" -Arguments @("info", "--format", "{{.ServerVersion}}") `
        -FailureMessage "Docker Desktop 未运行、引擎未就绪或当前用户无法访问 Docker" `
        -TimeoutSeconds 20
} catch {
    try {
        Repair-DockerDesktopStartup -OriginalFailure $_.Exception.Message
    } catch {
        Stop-WithError $_.Exception.Message
    }
}

try {
    Select-LocalRedisImage
} catch {
    Stop-WithError $_.Exception.Message
}

$venvStatus = Get-VirtualEnvironmentStatus -Root $venvRoot -PythonPath $venvPython `
    -RequiredVersion "3.12"
if (-not $venvStatus.Valid) {
    if ($NoInstall) {
        Stop-WithError "$($venvStatus.Reason)。-NoInstall 禁止修复虚拟环境；请移除该参数后重试"
    }
    Require-Command -Name "python" -InstallHint "请安装 Python 3.12 x64 并加入 PATH。"
    $pythonVersion = & python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
    if ($pythonVersion -ne "3.12") {
        Stop-WithError "需要 Python 3.12，当前为 $pythonVersion"
    }
    if ($venvStatus.Exists) {
        $venvBackup = Move-InvalidVirtualEnvironmentToBackup -Root $venvRoot
        Write-Info "检测到无效虚拟环境：$($venvStatus.Reason)"
        Write-Info "旧环境已保留到 $venvBackup，可在确认新环境正常后自行清理"
    }
    Write-Info "创建 Python 虚拟环境"
    Invoke-Checked -FilePath "python" -Arguments @("-m", "venv", $venvRoot) `
        -FailureMessage "创建 Python 虚拟环境失败"
    $venvStatus = Get-VirtualEnvironmentStatus -Root $venvRoot -PythonPath $venvPython `
        -RequiredVersion "3.12"
    if (-not $venvStatus.Valid) {
        Stop-WithError "新建虚拟环境不可用：$($venvStatus.Reason)"
    }
}

if (-not $NoInstall) {
    Write-Info "安装 API 与 Worker（editable）"
    $apiPackage = (Join-Path $projectRoot "apps\api") + "[dev]"
    $workerPackage = Join-Path $projectRoot "services\worker"
    Invoke-Checked -FilePath $venvPython -Arguments @(
        "-m", "pip", "install", "--disable-pip-version-check", "-e", $apiPackage, "-e", $workerPackage
    ) -FailureMessage "Python 依赖安装失败"

    if ($BrowserCapture) {
        Write-Info "安装锁定的 Playwright 截图运行时"
        Invoke-Checked -FilePath $venvPython -Arguments @(
            "-m", "pip", "install", "--disable-pip-version-check", "--require-hashes",
            "-r", (Join-Path $projectRoot "services\worker\requirements-browser.txt")
        ) -FailureMessage "Playwright Python 依赖安装失败"
        Invoke-Checked -FilePath $venvPython -Arguments @(
            "-m", "playwright", "install", "chromium"
        ) -FailureMessage "Playwright Chromium 安装失败"
    }

    Write-Info "按锁文件校验 Web 依赖"
    Push-Location $webRoot
    try {
        Invoke-Checked -FilePath "npm" -Arguments @("ci") -FailureMessage "Web 依赖安装失败"
    } finally {
        Pop-Location
    }
}

$env:POSTGRES_DB = $databaseName
$env:POSTGRES_USER = $databaseUser
$env:POSTGRES_PASSWORD = $databasePassword
$env:POSTGRES_PORT = "$postgresPort"
$env:REDIS_PORT = "$redisPort"
$env:MINIO_API_PORT = "$minioApiPort"
$env:MINIO_CONSOLE_PORT = "$minioConsolePort"
$env:FRAMEFACTORY_S3_BUCKET = $s3Bucket
$env:FRAMEFACTORY_S3_ACCESS_KEY_ID = $s3AccessKey
$env:FRAMEFACTORY_S3_SECRET_ACCESS_KEY = $s3SecretKey
Write-Info "启动 Vistora 独立 PostgreSQL、Redis 与 MinIO"
$env:FRAMEFACTORY_REDIS_PROTECTED_MODE = "no"
foreach ($volumeName in @(
    "vistora-local_vistora-postgres",
    "vistora-local_vistora-redis",
    "vistora-local_vistora-minio"
)) {
    Invoke-Checked -FilePath "docker" -Arguments @(
        "volume", "create", $volumeName
    ) -FailureMessage "创建或确认持久化卷 $volumeName 失败" -TimeoutSeconds 20 -CaptureOutput | Out-Null
}
Invoke-Checked -FilePath "docker" -Arguments @(
    "compose", "-f", $composePath, "up", "-d", "--wait", "postgres", "redis", "minio"
) -FailureMessage "持久化服务启动失败" -TimeoutSeconds 180
Invoke-Checked -FilePath "docker" -Arguments @(
    "compose", "-f", $composePath, "run", "--rm", "minio-init"
) -FailureMessage "对象存储初始化失败" -TimeoutSeconds 120

$databaseExists = ([string](Invoke-Checked -FilePath "docker" -Arguments @(
    "compose", "-f", $composePath, "exec", "-T", "postgres",
    "psql", "-U", $databaseUser, "-d", "postgres", "-tAc",
    "SELECT 1 FROM pg_database WHERE datname='$databaseName'"
) -FailureMessage "检查 Vistora 数据库失败" -TimeoutSeconds 30 -CaptureOutput)).Trim()
if ($databaseExists -ne "1") {
    Write-Info "创建 Vistora 数据库 $databaseName"
    Invoke-Checked -FilePath "docker" -Arguments @(
        "compose", "-f", $composePath, "exec", "-T", "postgres",
        "createdb", "-U", $databaseUser, $databaseName
    ) -FailureMessage "Vistora 数据库创建失败" -TimeoutSeconds 30
}

$env:FRAMEFACTORY_ENV = "development"
$env:FRAMEFACTORY_REPOSITORY_BACKEND = "postgresql"
$env:FRAMEFACTORY_DATABASE_URL = "postgresql://${databaseUser}:${databasePassword}@127.0.0.1:${postgresPort}/$databaseName"
$env:FRAMEFACTORY_REDIS_ENABLED = "true"
$env:FRAMEFACTORY_REDIS_URL = "redis://127.0.0.1:${redisPort}/0"
$env:FRAMEFACTORY_REDIS_NAMESPACE = $redisNamespace
$env:FRAMEFACTORY_WORKER_CONCURRENCY = "2"
$env:FRAMEFACTORY_WORKER_LEASE_SECONDS = "300"
$env:FRAMEFACTORY_OBJECT_STORAGE_ENABLED = "true"
$env:FRAMEFACTORY_S3_ENDPOINT_URL = "http://127.0.0.1:${minioApiPort}"
$env:FRAMEFACTORY_S3_BUCKET = $s3Bucket
$env:FRAMEFACTORY_S3_REGION = "us-east-1"
$env:FRAMEFACTORY_S3_ACCESS_KEY_ID = $s3AccessKey
$env:FRAMEFACTORY_S3_SECRET_ACCESS_KEY = $s3SecretKey
$env:FRAMEFACTORY_S3_ADDRESSING_STYLE = "path"
$env:FRAMEFACTORY_S3_VERIFY_TLS = "false"
$env:FRAMEFACTORY_S3_CREATE_BUCKET = "true"
if ($BrowserCapture) {
    $env:FRAMEFACTORY_S3_PUBLIC_ENDPOINT_URL = "http://127.0.0.1:${minioApiPort}"
    $env:FRAMEFACTORY_S3_ALLOW_INSECURE_LOOPBACK_PUBLIC_ENDPOINT = "true"
}
$env:FRAMEFACTORY_CORS_ALLOW_ORIGINS = "$webUrl,http://localhost:$WebPort"
$env:FRAMEFACTORY_CONTRACT_SCHEMA_DIR = Join-Path $projectRoot "packages\contracts\schemas\v1"
$env:FRAMEFACTORY_OFFICIAL_SEED_MANIFEST = Join-Path $projectRoot "packages\seeds\official-skills\v1\manifest.json"
$env:NEXT_PUBLIC_FRAMEFACTORY_API_URL = $apiUrl

Clear-ProviderEnvironment
Import-ProviderEnvironment -Path $ProviderEnvFile
if ($BenchmarkAnalysis) {
    # This mode starts research routes in this API. Override a stale .env.local
    # research URL so the Web cannot keep targeting a previous standalone API.
    $env:NEXT_PUBLIC_FRAMEFACTORY_BENCHMARK_API_URL = $apiUrl
    if ([string]::IsNullOrWhiteSpace($env:FRAMEFACTORY_XHS_MANAGED_BROWSER_BASE_URL)) {
        throw "Benchmark analysis requires FRAMEFACTORY_XHS_MANAGED_BROWSER_BASE_URL pointing to a running local browser provider. Connect Xiaohongshu from /benchmarks after startup. See docs/sop/benchmark-video-analysis.md."
    }
    if (-not $NoInstall) {
        Invoke-Checked -FilePath $venvPython -Arguments @(
            "-m", "pip", "install", "--require-hashes", "-r",
            (Join-Path $projectRoot "services\worker\requirements-benchmark-lock.txt")
        )
    }
    $env:FRAMEFACTORY_BENCHMARK_JOBS_DIR = Join-Path $projectRoot "var\benchmark-analysis"
    $env:FRAMEFACTORY_BENCHMARK_PROVIDER_ENV_FILE = [System.IO.Path]::GetFullPath($ProviderEnvFile)
} else {
    Remove-Item Env:FRAMEFACTORY_BENCHMARK_JOBS_DIR -ErrorAction SilentlyContinue
    Remove-Item Env:FRAMEFACTORY_BENCHMARK_PROVIDER_ENV_FILE -ErrorAction SilentlyContinue
}
if (
    [string]::IsNullOrWhiteSpace($env:FRAMEFACTORY_RESEARCH_SEARCH_BEARER_TOKEN) -and
    $env:FRAMEFACTORY_RESEARCH_SEARCH_BEARER_TOKEN_SOURCE -eq "FRAMEFACTORY_ASSET_VISION_API_KEY"
) {
    $env:FRAMEFACTORY_RESEARCH_SEARCH_BEARER_TOKEN = $env:FRAMEFACTORY_ASSET_VISION_API_KEY
}
if (
    [string]::IsNullOrWhiteSpace($env:FRAMEFACTORY_WAN_API_KEY) -and
    $env:FRAMEFACTORY_WAN_API_KEY_SOURCE -eq "FRAMEFACTORY_ASSET_VISION_API_KEY"
) {
    $env:FRAMEFACTORY_WAN_API_KEY = $env:FRAMEFACTORY_ASSET_VISION_API_KEY
}
if (
    [string]::IsNullOrWhiteSpace($env:FRAMEFACTORY_FULL_AI_VISION_API_KEY) -and
    $env:FRAMEFACTORY_FULL_AI_VISION_API_KEY_SOURCE -eq "FRAMEFACTORY_ASSET_VISION_API_KEY"
) {
    $env:FRAMEFACTORY_FULL_AI_VISION_API_KEY = $env:FRAMEFACTORY_ASSET_VISION_API_KEY
}

$mediaToolsReady = $false
if ((Get-Command "ffmpeg" -ErrorAction SilentlyContinue) -and
    (Get-Command "ffprobe" -ErrorAction SilentlyContinue)) {
    $mediaToolsReady = $true
    $env:FRAMEFACTORY_LEGACY_MEDIA_ENABLED = "true"
    $env:FRAMEFACTORY_LEGACY_TTS_VOICE = "zh-CN-YunjianNeural"
    $env:FRAMEFACTORY_LEGACY_TTS_RATE = "+25%"
    Write-Info "已启用 Edge TTS 与 FFmpeg 本地媒体适配器"
}
$documentToolsReady = (
    $mediaToolsReady -and
    (Get-Command "pdfinfo" -ErrorAction SilentlyContinue) -and
    (Get-Command "pdftoppm" -ErrorAction SilentlyContinue)
)
if ($documentToolsReady) {
    Write-Info "已启用受限 PDF 检查、页面证据提取与文档合成适配器"
}
$env:FRAMEFACTORY_ASSET_LIBRARY_ENABLED = "true"
$env:FRAMEFACTORY_ASSET_MINIMUM_SIMILARITY = "0.35"
$env:FRAMEFACTORY_ASSET_MAXIMUM_ASSETS = "12"
$env:FRAMEFACTORY_CONTROL_API_URL = $apiUrl
$env:FRAMEFACTORY_ASSET_ACQUISITION_TIMEOUT_SECONDS = "600"

$workerCapabilities = @()
$textProviderKeys = @(
    "FRAMEFACTORY_OPENAI_BASE_URL",
    "FRAMEFACTORY_OPENAI_API_KEY",
    "FRAMEFACTORY_OPENAI_RESEARCH_MODEL",
    "FRAMEFACTORY_OPENAI_WRITING_MODEL",
    "FRAMEFACTORY_OPENAI_QUALITY_MODEL"
)
$textProviderReady = $true
foreach ($name in $textProviderKeys) {
    if ([string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($name))) {
        $textProviderReady = $false
    }
}
if ($textProviderReady) {
    $workerCapabilities += @(
        "writing.compose",
        "writing.compose.webpage",
        "writing.compose.webpage_story",
        "writing.compose.generated",
        "quality.evaluate",
        "model.text_generation"
    )
}
$researchSearchReady = (
    -not [string]::IsNullOrWhiteSpace($env:FRAMEFACTORY_RESEARCH_SEARCH_URL) -and
    -not [string]::IsNullOrWhiteSpace($env:FRAMEFACTORY_RESEARCH_SEARCH_BEARER_TOKEN)
)
if ($textProviderReady -and $researchSearchReady) {
    $workerCapabilities += "research.collect"
}
if ($mediaToolsReady) {
    $workerCapabilities += @(
        "audio.synthesize",
        "render.compose",
        "render.edl",
        "render.subtitle_sentence"
    )
}
if ($documentToolsReady -and $env:FRAMEFACTORY_OBJECT_STORAGE_ENABLED -eq "true") {
    $workerCapabilities += @(
        "document.inspect",
        "document.extract",
        "writing.compose.document",
        "document.storyboard.plan",
        "document.materialize",
        "media.augment",
        "document.timeline.align",
        "render.composite",
        "quality.evaluate.document"
    )
}
if ($env:FRAMEFACTORY_ASSET_LIBRARY_ENABLED -eq "true") {
    $workerCapabilities += @("media.select", "media.inventory", "media.retrieve")
}
if ($env:FRAMEFACTORY_OBJECT_STORAGE_ENABLED -eq "true") {
    $workerCapabilities += @(
        "timeline.align",
        "web.materialize",
        "web.region.analyze",
        "web.storyboard.plan",
        "web.materialize.regions"
    )
}
if ($BrowserCapture) {
    $workerCapabilities += @(
        "web.capture.validate",
        "web.capture.screenshot",
        "web.site.discover",
        "web.page.capture_batch"
    )
}
$fullAiVerificationKeys = @(
    "FRAMEFACTORY_FULL_AI_VISION_BASE_URL",
    "FRAMEFACTORY_FULL_AI_VISION_API_KEY",
    "FRAMEFACTORY_FULL_AI_VISION_MODEL"
)
$fullAiProvidersReady = $true
foreach ($name in $fullAiVerificationKeys) {
    if ([string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($name))) {
        $fullAiProvidersReady = $false
    }
}
if ($env:FRAMEFACTORY_FULL_AI_PROVIDER_NAME -eq "runway") {
    foreach ($name in @(
        "FRAMEFACTORY_RUNWAY_BASE_URL",
        "FRAMEFACTORY_RUNWAY_API_KEY",
        "FRAMEFACTORY_RUNWAY_MODEL"
    )) {
        if ([string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($name))) {
            $fullAiProvidersReady = $false
        }
    }
} elseif ($env:FRAMEFACTORY_FULL_AI_PROVIDER_NAME -eq "dashscope-wan") {
    foreach ($name in @(
        "FRAMEFACTORY_WAN_BASE_URL",
        "FRAMEFACTORY_WAN_API_KEY",
        "FRAMEFACTORY_WAN_MODEL",
        "FRAMEFACTORY_WAN_COST_PER_SECOND_MINOR"
    )) {
        if ([string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($name))) {
            $fullAiProvidersReady = $false
        }
    }
} else {
    $fullAiProvidersReady = $false
}
$fullAiControlKeys = @(
    "FRAMEFACTORY_FULL_AI_PROVIDER_NAME",
    "FRAMEFACTORY_FULL_AI_MODEL_ID",
    "FRAMEFACTORY_FULL_AI_COST_PER_SECOND_MINOR",
    "FRAMEFACTORY_FULL_AI_CREDIT_UNIT_MINOR",
    "FRAMEFACTORY_FULL_AI_TERMS_REFERENCE",
    "FRAMEFACTORY_FULL_AI_TERMS_CONTENT_HASH",
    "FRAMEFACTORY_FULL_AI_TERMS_CAPTURED_AT",
    "FRAMEFACTORY_FULL_AI_PRICING_REFERENCE",
    "FRAMEFACTORY_FULL_AI_PRICING_CONTENT_HASH",
    "FRAMEFACTORY_FULL_AI_PRICING_CAPTURED_AT",
    "FRAMEFACTORY_FULL_AI_OUTPUT_RIGHTS_CONFIRMED",
    "FRAMEFACTORY_FULL_AI_OUTPUT_RIGHTS_LICENSE_BASIS"
)
$fullAiControlReady = $true
foreach ($name in $fullAiControlKeys) {
    if ([string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($name))) {
        $fullAiControlReady = $false
    }
}
$fullAiIdentityReady = (
    (
        ($env:FRAMEFACTORY_FULL_AI_PROVIDER_NAME -eq "runway" -and
            $env:FRAMEFACTORY_FULL_AI_MODEL_ID -eq $env:FRAMEFACTORY_RUNWAY_MODEL) -or
        ($env:FRAMEFACTORY_FULL_AI_PROVIDER_NAME -eq "dashscope-wan" -and
            $env:FRAMEFACTORY_FULL_AI_MODEL_ID -eq $env:FRAMEFACTORY_WAN_MODEL -and
            $env:FRAMEFACTORY_FULL_AI_COST_PER_SECOND_MINOR -eq
                $env:FRAMEFACTORY_WAN_COST_PER_SECOND_MINOR)
    ) -and
    $env:FRAMEFACTORY_FULL_AI_CREDIT_UNIT_MINOR -eq "1" -and
    $env:FRAMEFACTORY_FULL_AI_OUTPUT_RIGHTS_CONFIRMED -eq "true"
)
$fullAiExpectedReady = (
    $fullAiProvidersReady -and $fullAiControlReady -and $fullAiIdentityReady -and
    $textProviderReady -and
    $mediaToolsReady -and
    $env:FRAMEFACTORY_OBJECT_STORAGE_ENABLED -eq "true"
)
if ($fullAiExpectedReady) {
    $workerCapabilities += @(
        "media.generate",
        "model.video_generation",
        "model.generated_video_verification"
    )
}
$env:FRAMEFACTORY_WORKER_CAPABILITIES = ($workerCapabilities | Sort-Object -Unique) -join ","
Write-Info "本次 Worker 能力：$env:FRAMEFACTORY_WORKER_CAPABILITIES"

$migrationProviderSecrets = Suspend-WorkerProviderSecrets
Write-Info "执行数据库迁移与校验"
Invoke-Checked -FilePath $venvPython -Arguments @(
    "-m", "framefactory_api.migrate", "--project-root", $projectRoot
) -FailureMessage "数据库迁移失败"
Restore-WorkerProviderSecrets -Secrets $migrationProviderSecrets

# Validate Worker config, database/ledger, Redis and object storage before an
# API process can advertise this launcher's Full-AI readiness.
Invoke-Checked -FilePath $venvPython -Arguments @(
    "-m", "framefactory.worker", "healthcheck"
) -FailureMessage "Worker 无法连接 PostgreSQL、Redis、对象存储或付费账本"

$managed = @()
$apiProcessRecord = $null
$webProcessRecord = $null
$workerProviderSecrets = Suspend-WorkerProviderSecrets
$apiReused = Assert-PortAvailableOrHealthy -Port $ApiPort -HealthUrl "$apiUrl/healthz" `
    -Name "Control API" -RequireDurableApi
if ($apiReused) {
    Restore-WorkerProviderSecrets -Secrets $workerProviderSecrets
    Stop-WithError "检测到未知来源的 Control API；禁止复用外部进程，请先停止占用 8200 端口的服务"
}
$webReused = Assert-PortAvailableOrHealthy -Port $WebPort -HealthUrl $webUrl -Name "Web"
try {
    Write-Info "启动 Control API"
    $apiProcessRecord = Start-ManagedProcess -Name "api" -FilePath $venvPython -Arguments @(
        "-m", "uvicorn", "framefactory_api.main:app", "--host", "127.0.0.1", "--port", "$ApiPort"
    ) -WorkingDirectory $projectRoot
    $managed += $apiProcessRecord
    Save-ManagedProcessState -Processes $managed
    Restore-WorkerProviderSecrets -Secrets $workerProviderSecrets
    Wait-Http -Url "$apiUrl/readyz" -Name "Control API" -ProcessId $apiProcessRecord.pid
    Assert-FullAiApiConfiguration -ExpectedReady $fullAiExpectedReady `
        -ExpectedProvider $env:FRAMEFACTORY_FULL_AI_PROVIDER_NAME `
        -ExpectedModel $env:FRAMEFACTORY_FULL_AI_MODEL_ID
    if ($BenchmarkAnalysis) {
        Invoke-Checked -FilePath $venvPython -Arguments @(
            (Join-Path $projectRoot "tools\verify_benchmark_runtime.py"),
            "--api-url", $apiUrl, "--web-origin", $webUrl
        ) -TimeoutSeconds 55 -FailureMessage "研究 API 接口或配置不兼容，请检查并更新 API/Web；此检查不会生成二维码"
    }

    # Provider credentials use the supported v3 environment contract documented in
    # services/worker/.env.example. The launcher only reads the ignored v3 secret
    # file selected by -ProviderEnvFile and never reads the retired src/ tree.

    Write-Info "启动 Worker"
    $managed += Start-ManagedProcess -Name "worker" -FilePath $venvPython -Arguments @(
        "-m", "framefactory.worker", "run"
    ) -WorkingDirectory $projectRoot
    Save-ManagedProcessState -Processes $managed
    Clear-ProviderEnvironment

    if ($BrowserCapture) {
        Remove-Item Env:FRAMEFACTORY_ASSET_LIBRARY_ENABLED -ErrorAction SilentlyContinue
        Remove-Item Env:FRAMEFACTORY_CONTROL_API_URL -ErrorAction SilentlyContinue
        Write-Info "启动本地受限截图出站代理"
        $managed += Start-ManagedProcess -Name "browser-egress-proxy" `
            -FilePath $venvPython -Arguments @(
                "-m", "framefactory.worker.web_capture.development_proxy",
                "--host", "127.0.0.1", "--port", "$browserProxyPort"
            ) -WorkingDirectory $projectRoot
        Save-ManagedProcessState -Processes $managed
        Wait-Tcp -Address "127.0.0.1" -Port $browserProxyPort `
            -Name "Browser egress proxy"

        $env:FRAMEFACTORY_STEP_QUEUE = "browser-capture"
        $env:FRAMEFACTORY_WORKER_ID = "browser-capture-local"
        $env:FRAMEFACTORY_WORKER_CONCURRENCY = "1"
        $env:FRAMEFACTORY_BROWSER_CAPTURE_ONLY = "true"
        $env:FRAMEFACTORY_BROWSER_CAPTURE_ENABLED = "true"
        $env:FRAMEFACTORY_BROWSER_EGRESS_POLICY_ENFORCED = "true"
        $env:FRAMEFACTORY_BROWSER_EGRESS_PROXY_URL = "http://127.0.0.1:${browserProxyPort}"
        $env:FRAMEFACTORY_BROWSER_ALLOW_INSECURE_LOOPBACK_PROXY = "true"
        # Windows local development cannot combine Chromium's Linux-style
        # sandbox flag with the explicit loopback proxy. Production remains
        # sandboxed in the dedicated container profile.
        $env:FRAMEFACTORY_BROWSER_DISABLE_CHROMIUM_SANDBOX = "true"
        $env:FRAMEFACTORY_BROWSER_ALLOW_NON_STANDARD_PORTS = "false"
        $env:FRAMEFACTORY_S3_KEY_PREFIX = "browser-capture"
        Write-Info "校验并启动独立 Browser Capture Worker"
        Invoke-Checked -FilePath $venvPython -Arguments @(
            "-m", "framefactory.worker", "healthcheck"
        ) -FailureMessage "Browser Capture Worker 自检失败"
        $managed += Start-ManagedProcess -Name "browser-capture-worker" `
            -FilePath $venvPython -Arguments @(
                "-m", "framefactory.worker", "run"
            ) -WorkingDirectory $projectRoot
        Save-ManagedProcessState -Processes $managed
        Clear-BrowserCaptureEnvironment
    }

    if (-not $webReused) {
        Write-Info "启动 Web"
        $webProcessRecord = Start-ManagedProcess -Name "web" -FilePath "npm.cmd" -Arguments @(
            "run", "dev", "--", "--hostname", "127.0.0.1", "--port", "$WebPort"
        ) -WorkingDirectory $webRoot
        $managed += $webProcessRecord
        Save-ManagedProcessState -Processes $managed
    }
    if ($webProcessRecord) {
        Wait-Http -Url $webUrl -Name "Web" -ProcessId $webProcessRecord.pid
    } else {
        Wait-Http -Url $webUrl -Name "Web"
    }

    Save-ManagedProcessState -Processes $managed
} catch {
    Restore-WorkerProviderSecrets -Secrets $workerProviderSecrets
    Clear-ProviderEnvironment
    Clear-BrowserCaptureEnvironment
    Stop-ManagedProcesses -Processes $managed
    Remove-Item -LiteralPath $statePath -Force -ErrorAction SilentlyContinue
    Stop-WithError $_.Exception.Message
}

Write-Good "Vistora 已启动：$webUrl/create"
Write-Host "日志：$logRoot"
Write-Host "停止：.\stop.ps1（保留数据库）；.\stop.ps1 -Infrastructure（同时停止基础设施）"
if (-not $NoBrowser) {
    Start-Process "$webUrl/create"
}
