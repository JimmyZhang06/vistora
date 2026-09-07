# Shared Windows launcher primitives. Importing this file has no side effects.

function Suspend-StandardHandleInheritance {
    # PowerShell Start-Process can otherwise leak the launcher's caller pipes to
    # background children, keeping a redirected CMD invocation open after exit.
    if (-not ('VistoraLauncher.Handles' -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
namespace VistoraLauncher {
    public static class Handles {
        [DllImport("kernel32.dll")] public static extern IntPtr GetStdHandle(int id);
        [DllImport("kernel32.dll", SetLastError=true)]
        public static extern bool GetHandleInformation(IntPtr handle, out uint flags);
        [DllImport("kernel32.dll", SetLastError=true)]
        public static extern bool SetHandleInformation(IntPtr handle, uint mask, uint flags);
    }
}
'@
    }
    $saved = @()
    foreach ($id in @(-10, -11, -12)) {
        $handle = [VistoraLauncher.Handles]::GetStdHandle($id)
        [uint32]$flags = 0
        if ([VistoraLauncher.Handles]::GetHandleInformation($handle, [ref]$flags) -and ($flags -band 1)) {
            if (-not [VistoraLauncher.Handles]::SetHandleInformation($handle, 1, 0)) {
                throw '无法隔离后台进程的标准句柄，取消启动。'
            }
            $saved += @{handle=$handle; flags=$flags}
        }
    }
    return $saved
}

function Restore-StandardHandleInheritance {
    param([object[]]$Handles)
    foreach ($saved in $Handles) {
        [void][VistoraLauncher.Handles]::SetHandleInformation($saved.handle, 1, $saved.flags -band 1)
    }
}

function Open-LauncherLease {
    param([string]$RuntimeRoot)
    New-Item -ItemType Directory -Force -Path $RuntimeRoot | Out-Null
    try {
        return [IO.File]::Open((Join-Path $RuntimeRoot 'launcher.lock'),
            [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
    } catch [IO.IOException] {
        throw '另一个启动或停止操作正在执行，请等它完成后重试。'
    }
}

function Test-LauncherProcess {
    param([object]$Record)
    $candidate = Get-Process -Id ([int]$Record.pid) -ErrorAction SilentlyContinue
    if (-not $candidate) { return $false }
    if ($null -ne $Record.started_at_filetime_utc) {
        # JSON keeps this Int64; an exact creation time rejects recycled PIDs.
        return $candidate.StartTime.ToFileTimeUtc() -eq [long]$Record.started_at_filetime_utc
    }
    return $false # Missing identity information is never enough to stop a PID.
}

function Test-LocalPortBindable {
    param([int]$Port)
    $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, $Port)
    try {
        $listener.Server.ExclusiveAddressUse = $true
        $listener.Start()
        return $true
    } catch [Net.Sockets.SocketException] {
        return $false
    } finally {
        $listener.Stop()
    }
}

function Resolve-LauncherPort {
    param([int]$Preferred, [bool]$Explicit, [string]$Name, [int[]]$Reserved = @())
    if ($Preferred -notin $Reserved -and (Test-LocalPortBindable -Port $Preferred)) {
        return $Preferred
    }
    if ($Explicit) {
        throw "$Name 端口 $Preferred 无法绑定（占用、重复或被 Windows 保留）。请指定其他端口。"
    }
    # Deterministic fallback avoids Windows excluded ranges without probing hundreds
    # of ports or changing a caller's explicitly requested endpoint.
    $first = [Math]::Min(65504, $Preferred + 10000)
    foreach ($port in $first..($first + 31)) {
        if ($port -le 65535 -and $port -notin $Reserved -and (Test-LocalPortBindable $port)) {
            Write-Host "[i] $Name 默认端口 $Preferred 不可用，本次使用 $port"
            return $port
        }
    }
    throw "$Name 没有可用的本机端口，请显式指定端口。"
}

function Get-DescendantProcessIds {
    param([int]$RootProcessId)
    $all = @(Get-CimInstance Win32_Process -ErrorAction Stop)
    $result = [Collections.Generic.HashSet[int]]::new()
    $frontier = [Collections.Generic.Queue[int]]::new()
    $frontier.Enqueue($RootProcessId)
    while ($frontier.Count -gt 0) {
        $parent = $frontier.Dequeue()
        foreach ($child in $all | Where-Object { [int]$_.ParentProcessId -eq $parent }) {
            if ($result.Add([int]$child.ProcessId)) { $frontier.Enqueue([int]$child.ProcessId) }
        }
    }
    return @($result)
}

function ConvertTo-ProcessArgument {
    param([AllowEmptyString()][string]$Value)
    # CommandLineToArgvW quoting: Start-Process joins ArgumentList with spaces.
    # Quotes and trailing backslashes must therefore be escaped explicitly.
    return '"' + [regex]::Replace([regex]::Replace($Value, '(\\*)"', '$1$1\"'),
        '(\\+)$', '$1$1') + '"'
}

function Get-LauncherSourceStamp {
    param([string]$ProjectRoot, [string]$ProviderFile)
    $files = [Collections.Generic.List[string]]::new()
    foreach ($relative in @('start.ps1','stop.ps1','start.cmd','stop.cmd',
        'tools/local-launcher.ps1','tools/bootstrap-powershell.ps1',
        'apps/api/pyproject.toml','services/worker/pyproject.toml',
        'apps/web/package.json','apps/web/package-lock.json','apps/web/.env.local',
        'apps/web/vite.config.ts','apps/web/wrangler.jsonc','apps/web/tsconfig.json',
        'deploy/docker-compose.persistence.yml')) {
        $path = Join-Path $ProjectRoot $relative
        if (Test-Path -LiteralPath $path -PathType Leaf) { $files.Add($path) }
    }
    foreach ($relative in @('apps/api/src','services/worker/framefactory',
        'apps/web/app','apps/web/components','apps/web/lib')) {
        Get-ChildItem -LiteralPath (Join-Path $ProjectRoot $relative) -Recurse -File |
            Where-Object { $_.Extension -in @('.py','.ts','.tsx','.css','.mjs') } |
            ForEach-Object { $files.Add($_.FullName) }
    }
    $parts = @($files | Sort-Object | ForEach-Object {
        $relative = [IO.Path]::GetRelativePath($ProjectRoot, $_)
        "$relative=$((Get-FileHash -LiteralPath $_ -Algorithm SHA256).Hash)"
    })
    # No provider secret or secret hash is persisted. A changed config timestamp
    # invalidates reuse; provider values are only read for a fresh launch.
    if (Test-Path -LiteralPath $ProviderFile -PathType Leaf) {
        $configFile = Get-Item -LiteralPath $ProviderFile
        $parts += "provider=$($configFile.LastWriteTimeUtc.Ticks):$($configFile.Length)"
    }
    foreach ($tool in @('node','ffmpeg','ffprobe','pdfinfo','pdftoppm')) {
        $resolved = Get-Command $tool -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        $parts += "tool:${tool}=$($resolved.Source)"
    }
    $bytes = [Text.Encoding]::UTF8.GetBytes(($parts -join "`n"))
    return [Convert]::ToHexString([Security.Cryptography.SHA256]::HashData($bytes))
}

function Write-LauncherState {
    param([string]$Path, [object]$State)
    $temporary = "$Path.$([guid]::NewGuid().ToString('N')).tmp"
    try {
        [IO.File]::WriteAllText($temporary, ($State | ConvertTo-Json -Depth 8),
            [Text.UTF8Encoding]::new($false))
        [IO.File]::Move($temporary, $Path, $true)
    } finally {
        if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary }
    }
}

function Install-WebDependencies {
    $lockFile = Join-Path $webRoot 'package-lock.json'
    $installedLock = Join-Path $webRoot 'node_modules/.package-lock.json'
    $markerPath = Join-Path $runtimeRoot 'web-dependencies.json'
    $lockHash = (Get-FileHash -LiteralPath $lockFile -Algorithm SHA256).Hash
    if ((Test-Path -LiteralPath $markerPath) -and (Test-Path -LiteralPath $installedLock) -and
        (Test-Path -LiteralPath $vinextCli)) {
        $marker = Get-Content -LiteralPath $markerPath -Raw | ConvertFrom-Json
        if ($marker.lock_hash -eq $lockHash -and
            $marker.installed_hash -eq (Get-FileHash -LiteralPath $installedLock -Algorithm SHA256).Hash) {
            Write-Good 'Web 依赖与上次安装的锁文件一致，复用已有依赖'
            return
        }
    }
    Write-Info '按锁文件安装 Web 依赖'
    Push-Location $webRoot
    try {
        try {
            Invoke-Checked -FilePath 'npm' -Arguments @('ci', '--no-fund') -FailureMessage 'Web 依赖安装失败'
        } catch {
            # Windows can retain a native binding briefly after server shutdown.
            Write-Info '依赖安装未完成，等待文件句柄释放后重试一次'
            Start-Sleep -Seconds 2
            Invoke-Checked -FilePath 'npm' -Arguments @('ci', '--no-fund') -FailureMessage 'Web 依赖安装失败；请检查 npm 日志及文件占用'
        }
        Write-LauncherState -Path $markerPath -State @{
            lock_hash=$lockHash
            installed_hash=(Get-FileHash -LiteralPath $installedLock -Algorithm SHA256).Hash
        }
    } finally { Pop-Location }
}
