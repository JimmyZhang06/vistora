<# Stop only processes recorded by start.ps1. Persistent volumes are preserved. #>
[CmdletBinding()]
param([switch]$Infrastructure)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$statePath = Join-Path $projectRoot "var\runtime\local-processes.json"
$composePath = Join-Path $projectRoot "deploy\docker-compose.persistence.yml"

function Get-DescendantProcessIds {
    param([int]$RootProcessId)
    $all = @(Get-CimInstance Win32_Process)
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

if (Test-Path -LiteralPath $statePath) {
    $state = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
    foreach ($entry in $state.processes) {
        $process = Get-Process -Id ([int]$entry.pid) -ErrorAction SilentlyContinue
        if (-not $process) { continue }
        $sameProcess = if ($null -ne $entry.started_at_filetime_utc) {
            [Math]::Abs(
                $process.StartTime.ToFileTimeUtc() - [long]$entry.started_at_filetime_utc
            ) -le [TimeSpan]::TicksPerSecond * 2
        } else {
            $actualStart = $process.StartTime
            $recordedStart = [DateTime]::Parse([string]$entry.started_at_utc)
            [Math]::Abs(($actualStart - $recordedStart).TotalSeconds) -le 2
        }
        if (-not $sameProcess) {
            Write-Warning "跳过 $($entry.name) PID $($entry.pid)：PID 已被其他进程复用"
            continue
        }
        $descendants = @(Get-DescendantProcessIds -RootProcessId $process.Id)
        if ($descendants.Count -gt 0) {
            Stop-Process -Id ($descendants | Sort-Object -Descending) -ErrorAction SilentlyContinue
        }
        Stop-Process -Id $process.Id -ErrorAction SilentlyContinue
        Write-Host "[OK] 已停止 $($entry.name) PID $($entry.pid)" -ForegroundColor Green
    }
    Remove-Item -LiteralPath $statePath
}

if ($Infrastructure) {
    & docker compose -f $composePath down
    if ($LASTEXITCODE -ne 0) { throw "停止持久化服务失败" }
    Write-Host "[OK] 已停止持久化服务；数据卷仍保留" -ForegroundColor Green
}
