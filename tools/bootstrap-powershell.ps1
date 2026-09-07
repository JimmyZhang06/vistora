#requires -Version 5.1
# Official portable distribution; no administrator rights or global PATH changes.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$root = Split-Path -Parent $PSScriptRoot
$runtimeRoot = Join-Path $root 'var\tools'
$destination = Join-Path $runtimeRoot 'powershell-7.6.5'
$lease = $null
try {
    New-Item -ItemType Directory -Force -Path $runtimeRoot | Out-Null
    $lease = [IO.File]::Open((Join-Path $runtimeRoot 'powershell-install.lock'), 'OpenOrCreate', 'ReadWrite', 'None')
    if (Test-Path -LiteralPath (Join-Path $destination 'pwsh.exe')) { exit 0 }
    if (Test-Path -LiteralPath $destination) { throw "Incomplete runtime at $destination; preserve it and rename it before retrying." }
    $archive = Join-Path $runtimeRoot ('powershell-' + [guid]::NewGuid().ToString('N') + '.zip')
    $staging = Join-Path $runtimeRoot ('powershell-' + [guid]::NewGuid().ToString('N') + '.staging')
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Write-Host '[i] Installing official PowerShell 7.6.5 in var/tools (first run only)...'
    Invoke-WebRequest -UseBasicParsing -TimeoutSec 300 -Uri 'https://github.com/PowerShell/PowerShell/releases/download/v7.6.5/PowerShell-7.6.5-win-x64.zip' -OutFile $archive
    $expected = '32eb8f6cdce08f86e987d625a2733e54ac3e289ae7e1621b14c0b5bcec2434ea'
    if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash -ne $expected) { throw 'PowerShell archive SHA256 mismatch; nothing was executed.' }
    Expand-Archive -LiteralPath $archive -DestinationPath $staging
    $candidate = Join-Path $staging 'pwsh.exe'
    if ((Get-AuthenticodeSignature -LiteralPath $candidate).Status -ne 'Valid') { throw 'PowerShell executable signature is invalid.' }
    # Both paths were created under this project's explicit runtime directory.
    if ([IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($staging)) -ne [IO.Path]::GetFullPath($runtimeRoot) -or
        [IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($destination)) -ne [IO.Path]::GetFullPath($runtimeRoot)) {
        throw 'Unexpected runtime path; refusing to move it.'
    }
    Move-Item -LiteralPath $staging -Destination $destination
    Write-Host '[OK] Portable PowerShell installed; the verified archive is retained in var/tools.'
} catch {
    Write-Host "[X] $($_.Exception.Message)"
    Write-Host 'Install PowerShell 7.2+ manually, or retry with access to github.com.'
    exit 1
} finally {
    if ($null -ne $lease) { $lease.Dispose() }
}
