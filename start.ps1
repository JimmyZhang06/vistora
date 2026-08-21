<#
  FrameFactory v3 local production-like launcher.

  Usage:
    .\start.ps1
    .\start.ps1 -NoBrowser
    .\start.ps1 -NoInstall

  The script starts the durable local stack: PostgreSQL, Redis, MinIO,
  migrations, Control API, Worker, and Web. Runtime files stay under var/.
#>

[CmdletBinding()]
param(
    [switch]$NoBrowser,
    [switch]$NoInstall,
    [string]$ProviderEnvFile = ""
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
if ([string]::IsNullOrWhiteSpace($ProviderEnvFile)) {
    $ProviderEnvFile = Join-Path $projectRoot "var\secrets\worker-provider.env"
}

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
    $allowed = @(
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
        "FRAMEFACTORY_ASR_BASE_URL",
        "FRAMEFACTORY_ASR_API_KEY",
        "FRAMEFACTORY_ASR_MODEL",
        "FRAMEFACTORY_ASR_TIMEOUT_SECONDS",
        "FRAMEFACTORY_ASR_RESPONSE_FORMAT",
        "FRAMEFACTORY_ASR_TIMESTAMP_MODE"
    )
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
    Stop-WithError "$Name 启动超时，请查看 $logRoot"
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

Require-Command -Name "python" -InstallHint "请安装 Python 3.12 x64 并加入 PATH。"
Require-Command -Name "node" -InstallHint "请安装 Node.js 22.13 或更高版本。"
Require-Command -Name "npm" -InstallHint "npm 应随 Node.js 一起安装。"
Require-Command -Name "docker" -InstallHint "请安装并启动 Docker Desktop。"

$pythonVersion = & python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
if ($pythonVersion -ne "3.12") {
    Stop-WithError "需要 Python 3.12，当前为 $pythonVersion"
}
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

if (-not (Test-Path -LiteralPath $venvPython)) {
    if ($NoInstall) { Stop-WithError "尚未创建 .venv，不能使用 -NoInstall" }
    Write-Info "创建 Python 虚拟环境"
    Invoke-Checked -FilePath "python" -Arguments @("-m", "venv", $venvRoot) `
        -FailureMessage "创建 Python 虚拟环境失败"
}

if (-not $NoInstall) {
    Write-Info "安装 API 与 Worker（editable）"
    $apiPackage = (Join-Path $projectRoot "apps\api") + "[dev]"
    $workerPackage = Join-Path $projectRoot "services\worker"
    Invoke-Checked -FilePath $venvPython -Arguments @(
        "-m", "pip", "install", "--disable-pip-version-check", "-e", $apiPackage, "-e", $workerPackage
    ) -FailureMessage "Python 依赖安装失败"

    Write-Info "按锁文件校验 Web 依赖"
    Push-Location $webRoot
    try {
        Invoke-Checked -FilePath "npm" -Arguments @("ci") -FailureMessage "Web 依赖安装失败"
    } finally {
        Pop-Location
    }
}

$env:FRAMEFACTORY_S3_BUCKET = "framefactory-v2"
Write-Info "启动 PostgreSQL、Redis 与 MinIO"
$env:FRAMEFACTORY_REDIS_PROTECTED_MODE = "no"
Invoke-Checked -FilePath "docker" -Arguments @(
    "compose", "-f", $composePath, "up", "-d", "--wait", "postgres", "redis", "minio"
) -FailureMessage "持久化服务启动失败"
Invoke-Checked -FilePath "docker" -Arguments @(
    "compose", "-f", $composePath, "run", "--rm", "minio-init"
) -FailureMessage "对象存储初始化失败"

$databaseName = "framefactory_v2"
$databaseExists = & docker compose -f $composePath exec -T postgres `
    psql -U framefactory -d postgres -tAc `
    "SELECT 1 FROM pg_database WHERE datname='$databaseName'" 2>$null | Out-String
if ($databaseExists.Trim() -ne "1") {
    Write-Info "创建新版数据库 $databaseName"
    Invoke-Checked -FilePath "docker" -Arguments @(
        "compose", "-f", $composePath, "exec", "-T", "postgres",
        "createdb", "-U", "framefactory", $databaseName
    ) -FailureMessage "新版数据库创建失败"
}

$env:FRAMEFACTORY_ENV = "development"
$env:FRAMEFACTORY_REPOSITORY_BACKEND = "postgresql"
$env:FRAMEFACTORY_DATABASE_URL = "postgresql://framefactory:framefactory-local-only@127.0.0.1:5432/$databaseName"
$env:FRAMEFACTORY_REDIS_ENABLED = "true"
$env:FRAMEFACTORY_REDIS_URL = "redis://127.0.0.1:6379/0"
$env:FRAMEFACTORY_REDIS_NAMESPACE = "framefactory-v2"
$env:FRAMEFACTORY_WORKER_CONCURRENCY = "2"
$env:FRAMEFACTORY_WORKER_LEASE_SECONDS = "300"
$env:FRAMEFACTORY_OBJECT_STORAGE_ENABLED = "true"
$env:FRAMEFACTORY_S3_ENDPOINT_URL = "http://127.0.0.1:9000"
$env:FRAMEFACTORY_S3_BUCKET = "framefactory-v2"
$env:FRAMEFACTORY_S3_REGION = "us-east-1"
$env:FRAMEFACTORY_S3_ACCESS_KEY_ID = "framefactory"
$env:FRAMEFACTORY_S3_SECRET_ACCESS_KEY = "framefactory-local-only"
$env:FRAMEFACTORY_S3_ADDRESSING_STYLE = "path"
$env:FRAMEFACTORY_S3_VERIFY_TLS = "false"
$env:FRAMEFACTORY_S3_CREATE_BUCKET = "true"
$env:FRAMEFACTORY_CORS_ALLOW_ORIGINS = "$webUrl,http://localhost:4173"
$env:FRAMEFACTORY_CONTRACT_SCHEMA_DIR = Join-Path $projectRoot "packages\contracts\schemas\v1"
$env:FRAMEFACTORY_OFFICIAL_SEED_MANIFEST = Join-Path $projectRoot "packages\seeds\official-skills\v1\manifest.json"
$env:NEXT_PUBLIC_FRAMEFACTORY_API_URL = $apiUrl

Import-ProviderEnvironment -Path $ProviderEnvFile

if ((Get-Command "ffmpeg" -ErrorAction SilentlyContinue) -and
    (Get-Command "ffprobe" -ErrorAction SilentlyContinue)) {
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
    $workerCapabilities += @("research.collect", "writing.compose", "quality.evaluate")
}
if ($env:FRAMEFACTORY_LEGACY_MEDIA_ENABLED -eq "true") {
    $workerCapabilities += @("audio.synthesize", "render.compose")
}
if ($env:FRAMEFACTORY_ASSET_LIBRARY_ENABLED -eq "true") {
    $workerCapabilities += "media.select"
}
$env:FRAMEFACTORY_WORKER_CAPABILITIES = ($workerCapabilities | Sort-Object -Unique) -join ","
Write-Info "本次 Worker 能力：$env:FRAMEFACTORY_WORKER_CAPABILITIES"

Write-Info "执行数据库迁移与校验"
Invoke-Checked -FilePath $venvPython -Arguments @(
    "-m", "framefactory_api.migrate", "--project-root", $projectRoot
) -FailureMessage "数据库迁移失败"

$managed = @()
$apiReused = Assert-PortAvailableOrHealthy -Port 8200 -HealthUrl "$apiUrl/healthz" `
    -Name "Control API" -RequireDurableApi
if (-not $apiReused) {
    Write-Info "启动 Control API"
    $managed += Start-ManagedProcess -Name "api" -FilePath $venvPython -Arguments @(
        "-m", "uvicorn", "framefactory_api.main:app", "--host", "127.0.0.1", "--port", "8200"
    ) -WorkingDirectory $projectRoot
}
Wait-Http -Url "$apiUrl/readyz" -Name "Control API"

# Provider credentials use the supported v3 environment contract documented in
# services/worker/.env.example. The launcher only reads the ignored v3 secret
# file selected by -ProviderEnvFile and never reads the retired src/ tree.

# Worker healthcheck must pass before the long-running process is launched.
Invoke-Checked -FilePath $venvPython -Arguments @(
    "-m", "framefactory.worker", "healthcheck"
) -FailureMessage "Worker 无法连接 PostgreSQL 或 Redis"
Write-Info "启动 Worker"
$managed += Start-ManagedProcess -Name "worker" -FilePath $venvPython -Arguments @(
    "-m", "framefactory.worker", "run"
) -WorkingDirectory $projectRoot
Remove-Item Env:FRAMEFACTORY_OPENAI_API_KEY -ErrorAction SilentlyContinue
Remove-Item Env:FRAMEFACTORY_OPENAI_BASE_URL -ErrorAction SilentlyContinue
Remove-Item Env:FRAMEFACTORY_OPENAI_RESEARCH_MODEL -ErrorAction SilentlyContinue
Remove-Item Env:FRAMEFACTORY_OPENAI_WRITING_MODEL -ErrorAction SilentlyContinue
Remove-Item Env:FRAMEFACTORY_OPENAI_QUALITY_MODEL -ErrorAction SilentlyContinue
Remove-Item Env:FRAMEFACTORY_OPENAI_TIMEOUT_SECONDS -ErrorAction SilentlyContinue
Remove-Item Env:FRAMEFACTORY_ASR_API_KEY -ErrorAction SilentlyContinue
Remove-Item Env:FRAMEFACTORY_ASR_BASE_URL -ErrorAction SilentlyContinue
Remove-Item Env:FRAMEFACTORY_ASR_MODEL -ErrorAction SilentlyContinue
Remove-Item Env:FRAMEFACTORY_ASR_RESPONSE_FORMAT -ErrorAction SilentlyContinue
Remove-Item Env:FRAMEFACTORY_ASR_TIMESTAMP_MODE -ErrorAction SilentlyContinue
Remove-Item Env:FRAMEFACTORY_ASR_TIMEOUT_SECONDS -ErrorAction SilentlyContinue
Remove-Item Env:FRAMEFACTORY_ASSET_VISION_API_KEY -ErrorAction SilentlyContinue
Remove-Item Env:FRAMEFACTORY_ASSET_VISION_BASE_URL -ErrorAction SilentlyContinue
Remove-Item Env:FRAMEFACTORY_ASSET_VISION_MODEL -ErrorAction SilentlyContinue

$webReused = Assert-PortAvailableOrHealthy -Port 4173 -HealthUrl $webUrl -Name "Web"
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

Write-Good "Vistora 已启动：$webUrl/create"
Write-Host "日志：$logRoot"
Write-Host "停止：.\stop.ps1（保留数据库）；.\stop.ps1 -Infrastructure（同时停止基础设施）"
if (-not $NoBrowser) {
    Start-Process "$webUrl/create"
}
