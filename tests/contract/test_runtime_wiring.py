from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_local_launcher_advertises_batch_inventory_capability() -> None:
    launcher = (REPO_ROOT / "start.ps1").read_text(encoding="utf-8")
    asset_library_block = re.search(
        r'if \(\$env:FRAMEFACTORY_ASSET_LIBRARY_ENABLED -eq "true"\) \{(?P<body>.*?)\n\}',
        launcher,
        flags=re.DOTALL,
    )

    assert asset_library_block is not None
    assert '"media.inventory"' in asset_library_block.group("body")


def test_local_launcher_accepts_research_search_provider_configuration() -> None:
    launcher = (REPO_ROOT / "start.ps1").read_text(encoding="utf-8")

    for setting in (
        "FRAMEFACTORY_RESEARCH_SEARCH_URL",
        "FRAMEFACTORY_RESEARCH_SEARCH_BEARER_TOKEN",
        "FRAMEFACTORY_RESEARCH_SEARCH_TIMEOUT_SECONDS",
        "FRAMEFACTORY_RESEARCH_SEARCH_MAX_RESPONSE_BYTES",
    ):
        assert f'"{setting}"' in launcher
    assert "if ($textProviderReady -and $researchSearchReady)" in launcher
    research_block = re.search(
        r"if \(\$textProviderReady -and \$researchSearchReady\) \{(?P<body>.*?)\n\}",
        launcher,
        flags=re.DOTALL,
    )
    assert research_block is not None
    assert '"research.collect"' in research_block.group("body")


def test_production_worker_uses_database_assets_and_search_provider() -> None:
    compose = (REPO_ROOT / "deploy" / "production" / "compose.yml").read_text(
        encoding="utf-8"
    )

    assert 'FRAMEFACTORY_ASSET_LIBRARY_ENABLED: "true"' in compose
    assert "FRAMEFACTORY_ASSET_VISION_API_KEY_FILE: /run/secrets/asset_vision_api_key" in compose
    assert "FRAMEFACTORY_ASR_API_KEY_FILE: /run/secrets/asr_api_key" in compose
    assert "FRAMEFACTORY_RESEARCH_SEARCH_URL: ${FF_RESEARCH_SEARCH_URL:?required}" in compose
    assert (
        "FRAMEFACTORY_RESEARCH_SEARCH_BEARER_TOKEN_FILE: "
        "/run/secrets/research_search_token"
    ) in compose
    assert 'research_search_token: {file: "${FF_RESEARCH_SEARCH_TOKEN_FILE:?required}"}' in compose
    assert "FRAMEFACTORY_CLAMD_HOST: clamav" in compose
    assert "clamav: {condition: service_healthy}" in compose
    assert "image: clamav/clamav:1.4.6" in compose


def test_local_launcher_bounds_docker_cli_calls() -> None:
    launcher = (REPO_ROOT / "start.ps1").read_text(encoding="utf-8")

    assert "$process.WaitForExit($TimeoutSeconds * 1000)" in launcher
    assert "$process.Kill($true)" in launcher
    assert '"docker" -Arguments @("info"' in launcher
    assert "-TimeoutSeconds 20" in launcher
    assert "& docker" not in launcher
    assert "检测到本项目已有 vinext dev 进程" in launcher
    assert "Win32_Process" in launcher
    assert "netstat.exe -ano -p tcp" in launcher
    assert '"volume", "create", $volumeName' in launcher


def test_local_launcher_supports_managed_frontend_only_mode() -> None:
    launcher = (REPO_ROOT / "start.ps1").read_text(encoding="utf-8")

    frontend = launcher.index("if ($FrontendOnly)")
    docker_requirement = launcher.index('Require-Command -Name "docker"')
    assert frontend < docker_requirement
    assert '[switch]$FrontendOnly' in launcher
    assert '[int]$WebPort = 4173' in launcher
    assert '$webUrl = "http://127.0.0.1:$WebPort"' in launcher
    assert '"--hostname", "127.0.0.1", "--port", "$WebPort"' in launcher
    assert '"--host", "127.0.0.1", "--port", "4173"' not in launcher
    assert "$env:NEXT_PUBLIC_FRAMEFACTORY_API_URL = $apiUrl" in launcher
    assert "Save-ManagedProcessState -Processes $managed" in launcher
    assert "Remove-Item -LiteralPath $statePath -Force" in launcher


def test_local_launcher_rejects_remote_docker_and_handles_empty_output() -> None:
    launcher = (REPO_ROOT / "start.ps1").read_text(encoding="utf-8")

    context_check = launcher.index('"context", "show"')
    volume_create = launcher.index('"volume", "create", $volumeName')
    assert context_check < volume_create
    assert '$dockerContext -ne "desktop-linux"' in launcher
    assert "避免误操作远程资源" in launcher
    for variable in ("officialRedisImage", "localRedisImage", "databaseExists"):
        assignment = re.search(
            rf"\${variable} = \(\[string\]\(Invoke-Checked .*?\)\)\.Trim\(\)",
            launcher,
            flags=re.DOTALL,
        )
        assert assignment is not None


def test_local_launcher_repairs_only_verified_socket_directories() -> None:
    launcher = (REPO_ROOT / "start.ps1").read_text(encoding="utf-8")

    assert '[switch]$NoDockerRepair' in launcher
    assert "function Repair-DockerDesktopStartup" in launcher
    assert "function Move-StaleDockerSocketDirectory" in launcher
    assert 'Join-Path $dockerParent "run"' in launcher
    assert 'Join-Path $localAppData "docker-secrets-engine"' in launcher
    assert "[IO.FileAttributes]::ReparsePoint" in launcher
    assert "包含非临时内容，拒绝自动修复" in launcher
    assert 'Start-Process -FilePath $desktopExe' in launcher
    assert '-WorkingDirectory $desktopRoot -WindowStyle Hidden' in launcher
    assert '"desktop", "start"' not in launcher
    assert '-TimeoutSeconds 3 -CaptureOutput -QuietFailure' in launcher
    assert '(Get-Command docker -CommandType Application | Select-Object -First 1).Source' in launcher
    assert '(Get-Command node -CommandType Application | Select-Object -First 1).Source' in launcher
    assert "Move-Item -LiteralPath $resolvedPath -Destination $destination" in launcher
    assert "Remove-Item -LiteralPath $resolvedPath" not in launcher

    repair_call = launcher.index(
        "Repair-DockerDesktopStartup -OriginalFailure $_.Exception.Message"
    )
    redis_selection = launcher.index("Select-LocalRedisImage", repair_call)
    assert repair_call < redis_selection


def test_local_launcher_checked_failures_are_catchable() -> None:
    launcher = (REPO_ROOT / "start.ps1").read_text(encoding="utf-8")
    invoke_checked = re.search(
        r"function Invoke-Checked \{(?P<body>.*?)\n\}",
        launcher,
        flags=re.DOTALL,
    )

    assert invoke_checked is not None
    body = invoke_checked.group("body")
    assert "throw" in body
    assert "Stop-WithError" not in body


def test_windows_stop_script_preserves_state_when_process_cannot_stop() -> None:
    stopper = (REPO_ROOT / "stop.ps1").read_text(encoding="utf-8")

    assert "taskkill.exe /PID $process.Id /T /F" in stopper
    assert "Get-CimInstance Win32_Process -ErrorAction Stop" in stopper
    assert "return @()" in stopper
    verify = stopper.index("Get-Process -Id $process.Id -ErrorAction SilentlyContinue")
    state_removal = stopper.index("Remove-Item -LiteralPath $statePath")
    assert verify < state_removal
    assert "状态文件已保留" in stopper


def test_persistence_volumes_are_explicit_external_dependencies() -> None:
    compose = (REPO_ROOT / "deploy" / "docker-compose.persistence.yml").read_text(
        encoding="utf-8"
    )

    assert compose.count("external: true") == 3
    for volume in (
        "vistora-local_vistora-postgres",
        "vistora-local_vistora-redis",
        "vistora-local_vistora-minio",
    ):
        assert f"name: {volume}" in compose


def test_local_launcher_full_ai_readiness_matches_control_plane_contract() -> None:
    launcher = (REPO_ROOT / "start.ps1").read_text(encoding="utf-8")
    required = {
        "model.text_generation",
        "model.video_generation",
        "model.generated_video_verification",
        "writing.compose.generated",
        "audio.synthesize",
        "media.generate",
        "timeline.align",
        "render.edl",
        "render.subtitle_sentence",
        "quality.evaluate",
    }
    for capability in required:
        assert f'"{capability}"' in launcher
    for setting in (
        "FRAMEFACTORY_RUNWAY_BASE_URL",
        "FRAMEFACTORY_RUNWAY_API_KEY",
        "FRAMEFACTORY_RUNWAY_MODEL",
        "FRAMEFACTORY_FULL_AI_VISION_BASE_URL",
        "FRAMEFACTORY_FULL_AI_VISION_API_KEY",
        "FRAMEFACTORY_FULL_AI_VISION_MODEL",
        "FRAMEFACTORY_FULL_AI_PROVIDER_NAME",
        "FRAMEFACTORY_FULL_AI_MODEL_ID",
        "FRAMEFACTORY_FULL_AI_TERMS_CONTENT_HASH",
        "FRAMEFACTORY_FULL_AI_PRICING_CONTENT_HASH",
        "FRAMEFACTORY_FULL_AI_OUTPUT_RIGHTS_CONFIRMED",
        "FRAMEFACTORY_FULL_AI_OUTPUT_RIGHTS_LICENSE_BASIS",
    ):
        assert f'"{setting}"' in launcher
    assert '$env:FRAMEFACTORY_FULL_AI_PROVIDER_NAME -eq "runway"' in launcher
    assert (
        "$env:FRAMEFACTORY_FULL_AI_MODEL_ID -eq "
        "$env:FRAMEFACTORY_RUNWAY_MODEL"
    ) in launcher
    for setting in (
        "FRAMEFACTORY_WAN_BASE_URL",
        "FRAMEFACTORY_WAN_API_KEY",
        "FRAMEFACTORY_WAN_MODEL",
        "FRAMEFACTORY_WAN_COST_PER_SECOND_MINOR",
    ):
        assert f'"{setting}"' in launcher
    assert '$env:FRAMEFACTORY_FULL_AI_PROVIDER_NAME -eq "dashscope-wan"' in launcher
    assert "$env:FRAMEFACTORY_FULL_AI_MODEL_ID -eq $env:FRAMEFACTORY_WAN_MODEL" in launcher


def test_local_launcher_isolates_api_secrets_and_healthchecks_worker_first() -> None:
    launcher = (REPO_ROOT / "start.ps1").read_text(encoding="utf-8")
    migration = launcher.index('"-m", "framefactory_api.migrate"')
    healthcheck = launcher.index('"-m", "framefactory.worker", "healthcheck"')
    suspend = launcher.index("$workerProviderSecrets = Suspend-WorkerProviderSecrets")
    api_start = launcher.index('Start-ManagedProcess -Name "api"')
    restore = launcher.index(
        "Restore-WorkerProviderSecrets -Secrets $workerProviderSecrets",
        api_start,
    )
    worker_start = launcher.index('Start-ManagedProcess -Name "worker"')

    assert migration < healthcheck < suspend < api_start < restore < worker_start
    suspend_function = re.search(
        r"function Suspend-WorkerProviderSecrets \{(?P<body>.*?)\n\}",
        launcher,
        flags=re.DOTALL,
    )
    assert suspend_function is not None
    for secret in (
        "FRAMEFACTORY_OPENAI_API_KEY",
        "FRAMEFACTORY_OPENAI_API_KEY_FILE",
        "FRAMEFACTORY_RUNWAY_API_KEY",
        "FRAMEFACTORY_RUNWAY_API_KEY_FILE",
        "FRAMEFACTORY_FULL_AI_VISION_API_KEY",
        "FRAMEFACTORY_FULL_AI_VISION_API_KEY_FILE",
    ):
        assert f'"{secret}"' in suspend_function.group("body")
    assert "Assert-FullAiApiConfiguration" in launcher
    assert '"$apiUrl/v1/full-ai/options"' in launcher
    assert "if ($apiReused)" in launcher
    assert "禁止复用外部进程" in launcher
    assert "Stop-ManagedProcesses -Processes $managed" in launcher
    assert "Get-DescendantProcessIds -RootProcessId $process.Id" in launcher
    assert "taskkill.exe /PID $process.Id /T /F" in launcher
    assert "Move-Item -LiteralPath $log -Destination" in launcher
    assert 'Join-Path $logRoot "archive"' in launcher
    assert "-ProcessId $apiProcessRecord.pid" in launcher
    assert "-ProcessId $webProcessRecord.pid" in launcher
    assert "Get-Process -Id $ProcessId -ErrorAction SilentlyContinue" in launcher
    state_write = launcher.index(
        "Save-ManagedProcessState -Processes $managed",
        worker_start,
    )
    rollback = launcher.index(
        "Stop-ManagedProcesses -Processes $managed",
        state_write,
    )
    assert worker_start < state_write < rollback

    clear_call = launcher.index("Clear-ProviderEnvironment\nImport-ProviderEnvironment")
    capability_calculation = launcher.index("$workerCapabilities = @()")
    assert clear_call < capability_calculation
    clear_function = re.search(
        r"function Clear-ProviderEnvironment \{(?P<body>.*?)\n\}",
        launcher,
        flags=re.DOTALL,
    )
    assert clear_function is not None
    assert 'Remove-Item -LiteralPath "Env:${name}_FILE"' in clear_function.group("body")
    assert "FRAMEFACTORY_LEGACY_MEDIA_ENABLED" in clear_function.group("body")
    assert "$mediaToolsReady" in launcher
