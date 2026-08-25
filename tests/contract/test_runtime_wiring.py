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
    rollback = launcher.index("Stop-ManagedProcesses -Processes $managed")
    state_write = launcher.index("Set-Content -LiteralPath $statePath")
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
