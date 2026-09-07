[CmdletBinding()]
param(
    [ValidateRange(1, 65535)]
    [int]$ApiPort = 8200,
    [ValidateRange(1, 65535)]
    [int]$WebPort = 4173,
    [string]$ProviderBaseUrl = "http://127.0.0.1:5556",
    [ValidateRange(1, 65535)]
    [int]$ProviderPort = 5556,
    [ValidateRange(5, 600)]
    [int]$ProviderWaitSeconds = 120,
    [ValidateRange(1, 120)]
    [int]$ProviderPollIntervalSeconds = 2,
    [string]$ProviderEnvFile = "var\\secrets\\worker-provider.env",
    [string]$ProviderHealthPath = "/browser/managed/status",
    [ValidateRange(1, 65535)]
    [int]$ProviderCdpPort = 9222,
    [switch]$NoInstall,
    [switch]$NoBrowser,
    [string]$ProviderLaunchCommand = "",
    [string]$ProviderLaunchCommandArgs = "",
    [switch]$SkipProviderWait
)

$ErrorActionPreference = "Stop"

function Write-Info { param([string]$Message) Write-Host "[i] $Message" -ForegroundColor Cyan }
function Write-Good { param([string]$Message) Write-Host "[OK] $Message" -ForegroundColor Green }
function Stop-WithError {
    param([string]$Message)
    Write-Host "[X] $Message" -ForegroundColor Red
    exit 1
}

function Resolve-ProviderCommand {
    $envCommand = [Environment]::GetEnvironmentVariable("FRAMEFACTORY_XHS_MANAGED_BROWSER_LAUNCH_COMMAND")
    $envArgs = [Environment]::GetEnvironmentVariable("FRAMEFACTORY_XHS_MANAGED_BROWSER_LAUNCH_ARGS")

    if ([string]::IsNullOrWhiteSpace($script:ProviderLaunchCommand) -and -not [string]::IsNullOrWhiteSpace($envCommand)) {
        $script:ProviderLaunchCommand = $envCommand.Trim()
    }
    if ([string]::IsNullOrWhiteSpace($script:ProviderLaunchCommandArgs) -and -not [string]::IsNullOrWhiteSpace($envArgs)) {
        $script:ProviderLaunchCommandArgs = $envArgs.Trim()
    }

}

function Parse-CommandArguments {
    param([string]$Value)
    if ([string]::IsNullOrWhiteSpace($Value)) {
        return @()
    }
    return [System.Management.Automation.PSParser]::Tokenize($Value, [ref]$null) |
        Select-Object -ExpandProperty Content
}

function Ensure-ProviderEnv {
    param([string]$Path, [string]$ProviderUrl)
    if (-not (Test-Path -LiteralPath $Path)) {
        Stop-WithError "未找到 Provider 配置文件：$Path"
    }

    $lines = Get-Content -Path $Path
    $key = "FRAMEFACTORY_XHS_MANAGED_BROWSER_BASE_URL"
    $newLine = "$key=$ProviderUrl"
    $linePattern = "^(?<key>[^=]+)="
    $updatedLines = [System.Collections.Generic.List[string]]::new()
    $hasKey = $false

    foreach ($line in $lines) {
        if ($line -match $linePattern) {
            $currentKey = $Matches["key"]
            if ($currentKey -eq $key) {
                if (-not $hasKey) {
                    $updatedLines.Add($newLine)
                    $hasKey = $true
                }
                continue
            }
        }
        $updatedLines.Add($line)
    }

    if (-not $hasKey) {
        $updatedLines.Add($newLine)
    }
    $updated = [string]::Join([Environment]::NewLine, $updatedLines)

    Set-Content -Path $Path -Value $updated -NoNewline
}

function Test-ProviderRunning {
    param([string]$ProviderUrl, [string]$HealthPath)

    $healthUrl = ("{0}{1}" -f $ProviderUrl.TrimEnd('/'), $HealthPath)
    try {
        $response = Invoke-RestMethod -Method Get -Uri $healthUrl -TimeoutSec 4 -ErrorAction Stop
        return $response.state -in @("running", "ready")
    } catch {
        return $false
    }
}

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$providerEnvPath = if ([System.IO.Path]::IsPathRooted($ProviderEnvFile)) {
    $ProviderEnvFile
} else {
    Join-Path $projectRoot $ProviderEnvFile
}
$apiUrl = "http://127.0.0.1:$ApiPort"
$webUrl = "http://127.0.0.1:$WebPort"

if ($ProviderPollIntervalSeconds -lt 1) {
    Stop-WithError "ProviderPollIntervalSeconds 必须大于 0"
}

try {
    $providerUri = [System.Uri]::new($ProviderBaseUrl)
    if ($providerUri.Port -ne $ProviderPort) {
        Write-Info "提示：当前 ProviderBaseUrl 端口与 ProviderPort 不一致，当前以 ProviderBaseUrl 为准"
    }
} catch {
    Write-Info "提示：无法解析 ProviderBaseUrl，按原值继续（ProviderPort 校验已跳过）"
}

Resolve-ProviderCommand

if ([string]::IsNullOrWhiteSpace($ProviderLaunchCommand) -and -not $SkipProviderWait) {
    # The standard launcher owns the real browser lifecycle. Never use a mock as
    # a fallback for login, and never rewrite the user's provider secrets here.
    & "$projectRoot\start.ps1" -BenchmarkAnalysis -ApiPort $ApiPort -WebPort $WebPort `
        -XhsBrowserPort $ProviderPort -ProviderEnvFile $providerEnvPath `
        -NoInstall:$NoInstall -NoBrowser:$NoBrowser
    return
}

if (-not [string]::IsNullOrWhiteSpace($ProviderLaunchCommand)) {
    Write-Info "检测到 Provider 启动命令参数，尝试启动受管浏览器服务..."
    try {
        $launchArgs = Parse-CommandArguments -Value $ProviderLaunchCommandArgs
        if (-not (Test-Path -LiteralPath $ProviderLaunchCommand -PathType Leaf)) {
            Stop-WithError "Provider 启动命令不存在：$ProviderLaunchCommand"
        }
        Start-Process -FilePath $ProviderLaunchCommand -ArgumentList $launchArgs -WindowStyle Hidden
    } catch {
        Stop-WithError "启动 Provider 失败：$($_.Exception.Message)"
    }
}

Ensure-ProviderEnv -Path $providerEnvPath -ProviderUrl $ProviderBaseUrl

if (-not $SkipProviderWait) {
    Write-Info "等待受管浏览器 Provider 就绪：$ProviderBaseUrl$ProviderHealthPath"
    $deadline = (Get-Date).AddSeconds($ProviderWaitSeconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-ProviderRunning -ProviderUrl $ProviderBaseUrl -HealthPath $ProviderHealthPath) {
            Write-Good "受管浏览器 Provider 已就绪：$ProviderBaseUrl"
            break
        }
        Start-Sleep -Seconds $ProviderPollIntervalSeconds
        Write-Host "  - 未检测到 Provider，${ProviderPollIntervalSeconds}s 后重试..."
    }

    if (-not (Test-ProviderRunning -ProviderUrl $ProviderBaseUrl -HealthPath $ProviderHealthPath)) {
        Stop-WithError "等待 Provider 超时（$ProviderWaitSeconds 秒）。请先启动受管浏览器服务（要求支持 GET /browser/managed/status 与 POST /xhs/login/qrcode）。"
    }
}

Write-Info "已写入 provider 配置：$providerEnvPath"
Write-Info "启动参数：API $apiUrl | Web $webUrl | 受管浏览器 $ProviderBaseUrl"

& "$projectRoot\start.ps1" -BenchmarkAnalysis -ExternalXhsBrowser -ProviderEnvFile $providerEnvPath `
    -ApiPort $ApiPort -WebPort $WebPort -NoInstall:$NoInstall -NoBrowser:$NoBrowser
