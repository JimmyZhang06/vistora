"""Exercise real Windows process/port/lock behavior without touching project services."""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PWSH = shutil.which("pwsh")
pytestmark = pytest.mark.skipif(os.name != "nt" or not PWSH, reason="Windows PowerShell 7 required")


def ps_string(value: object) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def run_ps(body: str) -> str:
    result = subprocess.run(
        [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command",
         "$ErrorActionPreference='Stop'; . " + ps_string(ROOT / "tools/local-launcher.ps1") + "; " + body],
        capture_output=True, text=True, encoding="utf-8", timeout=30, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout.strip()


def test_port_probe_rejects_occupied_port_and_explicit_port_does_not_fallback():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        output = run_ps(f"""
if (Test-LocalPortBindable {port}) {{ throw 'occupied port accepted' }}
try {{ Resolve-LauncherPort -Preferred {port} -Explicit $true -Name test; throw 'not rejected' }}
catch {{ if ($_.Exception.Message -notmatch '无法绑定') {{ throw }} }}
$fallback = Resolve-LauncherPort -Preferred {port} -Explicit $false -Name test
if ($fallback -eq {port} -or -not (Test-LocalPortBindable $fallback)) {{ throw 'invalid fallback' }}
Write-Output 'ok'
""")
        assert output.endswith("ok")


def test_port_probe_releases_socket():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    assert run_ps(f"Test-LocalPortBindable {port}") == "True"
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", port))


def test_launcher_lease_excludes_concurrent_writer_and_releases(tmp_path):
    run_ps(f"""
$first = Open-LauncherLease {ps_string(tmp_path)}
try {{
    try {{ $second = Open-LauncherLease {ps_string(tmp_path)}; $second.Dispose(); throw 'lock not exclusive' }}
    catch {{ if ($_.Exception.Message -notmatch '另一个启动或停止操作') {{ throw }} }}
}} finally {{ $first.Dispose() }}
$third = Open-LauncherLease {ps_string(tmp_path)}
$third.Dispose()
""")


def test_creation_time_rejects_reused_pid_and_state_preserves_int64(tmp_path):
    path = tmp_path / "state.json"
    run_ps(f"""
$record = @{{pid=$PID; started_at_filetime_utc=(Get-Process -Id $PID).StartTime.ToFileTimeUtc()}}
Write-LauncherState -Path {ps_string(path)} -State @{{processes=@($record)}}
$loaded = Get-Content -LiteralPath {ps_string(path)} -Raw | ConvertFrom-Json
if (-not (Test-LauncherProcess $loaded.processes[0])) {{ throw 'lost identity precision' }}
$loaded.processes[0].started_at_filetime_utc += 1
if (Test-LauncherProcess $loaded.processes[0]) {{ throw 'recycled pid accepted' }}
""")
    assert len(list(tmp_path.iterdir())) == 1


def test_start_process_quotes_unicode_spaces_quotes_and_trailing_backslash(tmp_path):
    script = tmp_path / "参数 空格.py"
    output = tmp_path / "结果.json"
    script.write_text("import json,sys; from pathlib import Path; Path(sys.argv[1]).write_text(json.dumps(sys.argv[2:]), encoding='utf-8')", encoding="utf-8")
    arguments = ["中文 空格", 'a"b', "trailing\\", "", 'a\\"b']
    all_args = [str(script), str(output), *arguments]
    run_ps(f"""
$arguments = @({','.join(ps_string(arg) for arg in all_args)})
$quoted = @($arguments | ForEach-Object {{ ConvertTo-ProcessArgument $_ }})
$process = Start-Process -FilePath {ps_string(sys.executable)} -ArgumentList $quoted -WindowStyle Hidden -PassThru -Wait
if ($process.ExitCode -ne 0) {{ throw 'child failed' }}
""")
    assert json.loads(output.read_text(encoding="utf-8")) == arguments


def test_background_process_does_not_keep_launcher_output_pipe_open(tmp_path):
    child = tmp_path / "background.py"
    identity = tmp_path / "pid.json"
    child.write_text("import time; time.sleep(60)", encoding="utf-8")
    try:
        run_ps(f"""
$tokens=$null; $errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile({ps_string(ROOT / 'start.ps1')},[ref]$tokens,[ref]$errors)
$definition=$ast.Find({{param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Start-ManagedProcess'}},$true)
Invoke-Expression $definition.Extent.Text
$logRoot={ps_string(tmp_path)}
$record=Start-ManagedProcess -Name test -FilePath {ps_string(sys.executable)} -Arguments @({ps_string(child)}) -WorkingDirectory {ps_string(tmp_path)}
Write-LauncherState -Path {ps_string(identity)} -State $record
Write-Output 'launcher returned while child remains alive'
""")
        record = json.loads(identity.read_text(encoding="utf-8"))
        assert run_ps(f"Test-LauncherProcess (Get-Content -LiteralPath {ps_string(identity)} -Raw | ConvertFrom-Json)") == "True"
        assert record["pid"] > 0
    finally:
        if identity.exists():
            run_ps(f"""
$record=Get-Content -LiteralPath {ps_string(identity)} -Raw | ConvertFrom-Json
if (Test-LauncherProcess $record) {{ Stop-Process -Id $record.pid -Force }}
""")
