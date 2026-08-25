<#
  Vistora local production-like launcher.

  Usage:
    .\start.ps1
    .\start.ps1 -NoBrowser
    .\start.ps1 -NoInstall
    .\start.ps1 -BrowserCapture

  The script starts the durable local stack: PostgreSQL, Redis, MinIO,
  migrations, Control API, Worker, and Web. Runtime files stay under var/.
#>

[CmdletBinding()]
param(
    [switch]$NoBrowser,
    [switch]$NoInstall,
    [switch]$BrowserCapture,
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
$apiUrl = "http://127.0.0.1:8200"
$webUrl = "http://localhost:4173"
$databaseName = $DatabaseName
$databaseUser = "vistora"
$databasePassword = "vistora-local-only"
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
    "FRAMEFACTORY_ASSET_VISION_BASE_URL",
    "FRAMEFACTORY_ASSET_VISION_API_KEY",
    "FRAMEFACTORY_ASSET_VISION_MODEL",
    "FRAMEFACTORY_ASSET_VISION_TIMEOUT_SECONDS",
    "FRAMEFACTORY_RUNWAY_BASE_URL",
    "FRAMEFACTORY_RUNWAY_API_KEY",
    "FRAMEFACTORY_RUNWAY_MODEL",
    "FRAMEFACTORY_RUNWAY_TIMEOUT_SECONDS",
    "FRAMEFACTORY_FULL_AI_VISION_BASE_URL",
    "FRAMEFACTORY_FULL_AI_VISION_API_KEY",
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
    "FRAMEFACTORY_ASR_TIMESTAMP_MODE"
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
    param([string]$FilePath, [string[]]$Arguments, [string]$FailureMessage)
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        Stop-WithError "$FailureMessage（退出码 $LASTEXITCODE）"
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
    param([string]$Url, [string]$Name, [int]$Attempts = 60)
    for ($attempt = 0; $attempt -lt $Attempts; $attempt++) {
        if (Test-Http $Url) {
            Write-Good "$Name 已就绪"
            return
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

Require-Command -Name "node" -InstallHint "请安装 Node.js 22.13 或更高版本。"
Require-Command -Name "npm" -InstallHint "npm 应随 Node.js 一起安装。"
Require-Command -Name "docker" -InstallHint "请安装并启动 Docker Desktop。"

$nodeMajor = [int]((& node -p "process.versions.node.split('.')[0]").Trim())
if ($nodeMajor -lt 22) {
    Stop-WithError "需要 Node.js 22 或更高版本，当前为 $(& node -v)"
}

Invoke-Checked -FilePath "docker" -Arguments @("info", "--format", "{{.ServerVersion}}") `
    -FailureMessage "Docker Desktop 未运行或当前用户无法访问 Docker"

if ([string]::IsNullOrWhiteSpace($env:FRAMEFACTORY_REDIS_IMAGE)) {
    $officialRedisImage = "$(& docker image ls --quiet `
        --filter 'reference=redis:7.4-alpine')".Trim()
    $localRedisImage = "$(& docker image ls --quiet `
        --filter 'reference=framefactory/redis-local:7.2.9-r0')".Trim()
    if (-not $officialRedisImage -and $localRedisImage) {
        $env:FRAMEFACTORY_REDIS_IMAGE = "framefactory/redis-local:7.2.9-r0"
        Write-Info "使用已验证的本机 Redis 开发镜像"
    }
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
    $all = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)
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
        $descendants = @(Get-DescendantProcessIds -RootProcessId $process.Id)
        if ($descendants.Count -gt 0) {
            Stop-Process -Id ($descendants | Sort-Object -Descending) -Force -ErrorAction SilentlyContinue
        }
        Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
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
Invoke-Checked -FilePath "docker" -Arguments @(
    "compose", "-f", $composePath, "up", "-d", "--wait", "postgres", "redis", "minio"
) -FailureMessage "持久化服务启动失败"
Invoke-Checked -FilePath "docker" -Arguments @(
    "compose", "-f", $composePath, "run", "--rm", "minio-init"
) -FailureMessage "对象存储初始化失败"

$databaseExists = & docker compose -f $composePath exec -T postgres `
    psql -U $databaseUser -d postgres -tAc `
    "SELECT 1 FROM pg_database WHERE datname='$databaseName'" 2>$null | Out-String
if ($databaseExists.Trim() -ne "1") {
    Write-Info "创建 Vistora 数据库 $databaseName"
    Invoke-Checked -FilePath "docker" -Arguments @(
        "compose", "-f", $composePath, "exec", "-T", "postgres",
        "createdb", "-U", $databaseUser, $databaseName
    ) -FailureMessage "Vistora 数据库创建失败"
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
$env:FRAMEFACTORY_CORS_ALLOW_ORIGINS = "$webUrl,http://localhost:4173"
$env:FRAMEFACTORY_CONTRACT_SCHEMA_DIR = Join-Path $projectRoot "packages\contracts\schemas\v1"
$env:FRAMEFACTORY_OFFICIAL_SEED_MANIFEST = Join-Path $projectRoot "packages\seeds\official-skills\v1\manifest.json"
$env:NEXT_PUBLIC_FRAMEFACTORY_API_URL = $apiUrl

Clear-ProviderEnvironment
Import-ProviderEnvironment -Path $ProviderEnvFile

$mediaToolsReady = $false
if ((Get-Command "ffmpeg" -ErrorAction SilentlyContinue) -and
    (Get-Command "ffprobe" -ErrorAction SilentlyContinue)) {
    $mediaToolsReady = $true
    $env:FRAMEFACTORY_LEGACY_MEDIA_ENABLED = "true"
    $env:FRAMEFACTORY_LEGACY_TTS_VOICE = "zh-CN-YunjianNeural"
    $env:FRAMEFACTORY_LEGACY_TTS_RATE = "+25%"
    Write-Info "已启用 Edge TTS 与 FFmpeg 本地媒体适配器"
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
        "research.collect",
        "writing.compose",
        "writing.compose.webpage",
        "writing.compose.webpage_story",
        "writing.compose.generated",
        "quality.evaluate",
        "model.text_generation"
    )
}
if ($mediaToolsReady) {
    $workerCapabilities += @(
        "audio.synthesize",
        "render.compose",
        "render.edl",
        "render.subtitle_sentence"
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
$fullAiProviderKeys = @(
    "FRAMEFACTORY_RUNWAY_BASE_URL",
    "FRAMEFACTORY_RUNWAY_API_KEY",
    "FRAMEFACTORY_RUNWAY_MODEL",
    "FRAMEFACTORY_FULL_AI_VISION_BASE_URL",
    "FRAMEFACTORY_FULL_AI_VISION_API_KEY",
    "FRAMEFACTORY_FULL_AI_VISION_MODEL"
)
$fullAiProvidersReady = $true
foreach ($name in $fullAiProviderKeys) {
    if ([string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($name))) {
        $fullAiProvidersReady = $false
    }
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
    $env:FRAMEFACTORY_FULL_AI_PROVIDER_NAME -eq "runway" -and
    $env:FRAMEFACTORY_FULL_AI_MODEL_ID -eq $env:FRAMEFACTORY_RUNWAY_MODEL -and
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
$workerProviderSecrets = Suspend-WorkerProviderSecrets
$apiReused = Assert-PortAvailableOrHealthy -Port 8200 -HealthUrl "$apiUrl/healthz" `
    -Name "Control API" -RequireDurableApi
if ($apiReused) {
    Restore-WorkerProviderSecrets -Secrets $workerProviderSecrets
    Stop-WithError "检测到未知来源的 Control API；禁止复用外部进程，请先停止占用 8200 端口的服务"
}
$webReused = Assert-PortAvailableOrHealthy -Port 4173 -HealthUrl $webUrl -Name "Web"
try {
    Write-Info "启动 Control API"
    $apiProcessRecord = Start-ManagedProcess -Name "api" -FilePath $venvPython -Arguments @(
        "-m", "uvicorn", "framefactory_api.main:app", "--host", "127.0.0.1", "--port", "8200"
    ) -WorkingDirectory $projectRoot
    $managed += $apiProcessRecord
    Restore-WorkerProviderSecrets -Secrets $workerProviderSecrets
    Wait-Http -Url "$apiUrl/readyz" -Name "Control API"
    Assert-FullAiApiConfiguration -ExpectedReady $fullAiExpectedReady `
        -ExpectedProvider $env:FRAMEFACTORY_FULL_AI_PROVIDER_NAME `
        -ExpectedModel $env:FRAMEFACTORY_FULL_AI_MODEL_ID

    # Provider credentials use the supported v3 environment contract documented in
    # services/worker/.env.example. The launcher only reads the ignored v3 secret
    # file selected by -ProviderEnvFile and never reads the retired src/ tree.

    Write-Info "启动 Worker"
    $managed += Start-ManagedProcess -Name "worker" -FilePath $venvPython -Arguments @(
        "-m", "framefactory.worker", "run"
    ) -WorkingDirectory $projectRoot
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
        Clear-BrowserCaptureEnvironment
    }

    if (-not $webReused) {
        Write-Info "启动 Web"
        $managed += Start-ManagedProcess -Name "web" -FilePath "npm.cmd" -Arguments @(
            "run", "dev", "--", "--host", "127.0.0.1", "--port", "4173"
        ) -WorkingDirectory $webRoot
    }
    Wait-Http -Url $webUrl -Name "Web"

    $state = [ordered]@{
        project_root = $projectRoot
        created_at_utc = [DateTime]::UtcNow.ToString("o")
        processes = $managed
    }
    $state | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $statePath -Encoding UTF8
} catch {
    Restore-WorkerProviderSecrets -Secrets $workerProviderSecrets
    Clear-ProviderEnvironment
    Clear-BrowserCaptureEnvironment
    Stop-ManagedProcesses -Processes $managed
    Stop-WithError $_.Exception.Message
}

Write-Good "Vistora 已启动：$webUrl/create"
Write-Host "日志：$logRoot"
Write-Host "停止：.\stop.ps1（保留数据库）；.\stop.ps1 -Infrastructure（同时停止基础设施）"
if (-not $NoBrowser) {
    Start-Process "$webUrl/create"
}
