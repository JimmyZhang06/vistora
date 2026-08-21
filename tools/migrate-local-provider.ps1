<#
  One-time local migration from the retired provider environment into the
  ignored v3 runtime boundary. The v3 launcher never reads the legacy file.
#>

[CmdletBinding()]
param(
    [string]$SourceEnv = "",
    [string]$DestinationEnv = "",
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
if ([string]::IsNullOrWhiteSpace($SourceEnv)) {
    $SourceEnv = Join-Path (Split-Path -Parent $projectRoot) "src\.env"
}
if ([string]::IsNullOrWhiteSpace($DestinationEnv)) {
    $DestinationEnv = Join-Path $projectRoot "var\secrets\worker-provider.env"
}
if (-not (Test-Path -LiteralPath $SourceEnv)) {
    throw "找不到旧版 Provider 配置：$SourceEnv"
}
if ((Test-Path -LiteralPath $DestinationEnv) -and -not $Force) {
    throw "新版 Provider 配置已存在；如需覆盖请显式使用 -Force"
}

$legacy = @{}
foreach ($rawLine in Get-Content -LiteralPath $SourceEnv) {
    $line = $rawLine.Trim()
    if (-not $line -or $line.StartsWith("#")) { continue }
    $parts = $line.Split("=", 2)
    if ($parts.Count -ne 2) { continue }
    $value = $parts[1].Trim()
    if (($value.StartsWith('"') -and $value.EndsWith('"')) -or
        ($value.StartsWith("'") -and $value.EndsWith("'"))) {
        $value = $value.Substring(1, $value.Length - 2)
    }
    $legacy[$parts[0].Trim()] = $value
}

foreach ($required in @("SILICONFLOW_API_KEY", "SILICONFLOW_BASE_URL", "LLM_MODEL")) {
    if ([string]::IsNullOrWhiteSpace([string]$legacy[$required])) {
        throw "旧版 Provider 配置缺少 $required"
    }
}
$baseUrl = [string]$legacy["SILICONFLOW_BASE_URL"]
if (-not $baseUrl.StartsWith("https://")) {
    throw "Provider Base URL 必须使用 HTTPS"
}

$destinationDirectory = Split-Path -Parent $DestinationEnv
New-Item -ItemType Directory -Force -Path $destinationDirectory | Out-Null
$model = [string]$legacy["LLM_MODEL"]
$lines = @(
    "# Generated once by tools/migrate-local-provider.ps1; never commit this file.",
    "FRAMEFACTORY_OPENAI_BASE_URL=$baseUrl",
    "FRAMEFACTORY_OPENAI_API_KEY=$($legacy['SILICONFLOW_API_KEY'])",
    "FRAMEFACTORY_OPENAI_RESEARCH_MODEL=$model",
    "FRAMEFACTORY_OPENAI_WRITING_MODEL=$model",
    "FRAMEFACTORY_OPENAI_QUALITY_MODEL=$model",
    "FRAMEFACTORY_OPENAI_TIMEOUT_SECONDS=120"
)
$lines | Set-Content -LiteralPath $DestinationEnv -Encoding UTF8
Write-Host "[OK] Provider 配置已迁移到 v3 私密运行目录；未输出任何密钥。" -ForegroundColor Green
