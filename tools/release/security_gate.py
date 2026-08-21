#!/usr/bin/env python3
"""Fail-closed release security and dependency gate for FrameFactory.

The gate intentionally scans every Git-tracked file for secrets. Production
configuration checks read the configured deployment paths, while dependency
audits cover only manifests included in the configured v3 release boundary. A JSON report is always
written, including when an audit tool is missing or cannot reach its registry.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

MAX_SCAN_BYTES = 2 * 1024 * 1024
PLACEHOLDER_MARKERS = (
    "${",
    "process.env",
    "os.environ",
    "replace-with",
    "replace_me",
    "changeme",
    "example",
    "placeholder",
    "dummy",
    "super-secret-value",
    "redacted",
    "<secret>",
    "/run/secrets/",
)

SECRET_PATTERNS = (
    ("private-key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{32,255}\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,255}\b")),
    ("openai-token", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,255}\b")),
    (
        "assigned-secret",
        re.compile(
            r"(?i)(?:api[_-]?key|client[_-]?secret|secret|token|password|passwd|private[_-]?key)"
            r"\s*[=:]\s*[\"']?([A-Za-z0-9_+./=-]{16,512})"
        ),
    ),
)

DANGEROUS_PATTERNS = (
    ("debug-enabled", re.compile(r"(?i)\b(?:debug|flask_debug)\s*[:=]\s*[\"']?(?:true|1|yes|on)\b")),
    (
        "development-mode",
        re.compile(r"(?i)\b(?:node_env|environment|app_env|flask_env)\s*[:=]\s*[\"']?(?:dev|development|local)\b"),
    ),
    (
        "wildcard-cors",
        re.compile(r"(?i)\b(?:cors[^\s:=]*|allowed[_-]?origins?)\s*[:=].*(?:[\"']\*[\"']|\[\s*\*\s*\])"),
    ),
    (
        "auth-not-required",
        re.compile(r"(?i)\b(?:auth|authentication|wallet_auth)[^\s:=]*\s*[:=]\s*[\"']?(?:off|false|none|disabled|optional)\b"),
    ),
    (
        "weak-password",
        re.compile(r"(?i)\b(?:password|passwd|secret)\s*[:=]\s*[\"']?(?:admin|password|changeme|changeit|default|secret|test|123456)\b"),
    ),
    ("tls-verification-disabled", re.compile(r"(?i)\b(?:verify_tls|tls_verify|ssl_verify)\s*[:=]\s*[\"']?false\b")),
    ("privileged-container", re.compile(r"(?i)^\s*privileged\s*:\s*true\s*$")),
    ("host-network", re.compile(r"(?i)^\s*network_mode\s*:\s*[\"']?host[\"']?\s*$")),
    (
        "secret-with-compose-default",
        re.compile(r"(?i)\$\{[^}]*(?:password|passwd|secret|token|api[_-]?key)[^}]*:-[^}]+}"),
    ),
)


@dataclass
class Check:
    name: str
    status: str
    summary: str
    findings: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


def run(command: list[str], cwd: Path, timeout: int = 600) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
        encoding="utf-8",
        errors="replace",
    )


def repository_root() -> Path:
    result = run(["git", "rev-parse", "--show-toplevel"], Path.cwd(), timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"not a Git worktree: {result.stderr.strip()}")
    return Path(result.stdout.strip()).resolve()


def tracked_files(root: Path) -> tuple[list[str], str | None]:
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=root,
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [], str(exc)
    if result.returncode != 0:
        return [], result.stderr.decode("utf-8", "replace").strip()
    return [item.decode("utf-8", "surrogateescape") for item in result.stdout.split(b"\0") if item], None


def read_text(path: Path) -> str | None:
    try:
        if not path.is_file() or path.stat().st_size > MAX_SCAN_BYTES:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if b"\0" in data:
        return None
    return data.decode("utf-8", "replace")


def is_placeholder(value: str, line: str) -> bool:
    lowered = f"{value} {line}".lower()
    if any(marker in lowered for marker in PLACEHOLDER_MARKERS):
        return True
    assignment = re.search(r"[=:]\s*(.+)$", line)
    if assignment:
        expression = assignment.group(1).strip()
        if "(" in expression and not expression.startswith(("'", '"', "b'", 'b"')):
            return True
        if re.match(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+[,;]?$", expression):
            return True
    return False


def fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:12]


def secret_check(root: Path, tracked: list[str], git_error: str | None) -> Check:
    if git_error:
        return Check("tracked-secrets", "failed", "Git tracked-file enumeration failed", metadata={"error": git_error})
    findings: list[dict[str, Any]] = []
    scanned = 0
    for relative in tracked:
        text = read_text(root / relative)
        if text is None:
            continue
        scanned += 1
        for line_number, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith(("#", "//")) and "PRIVATE KEY" not in line:
                continue
            for rule, pattern in SECRET_PATTERNS:
                match = pattern.search(line)
                if not match:
                    continue
                value = match.group(1) if match.lastindex else match.group(0)
                if is_placeholder(value, line):
                    continue
                findings.append(
                    {
                        "rule": rule,
                        "path": relative.replace(os.sep, "/"),
                        "line": line_number,
                        "fingerprint": fingerprint(value),
                    }
                )
    return Check(
        "tracked-secrets",
        "failed" if findings else "passed",
        f"{len(findings)} potential tracked secret(s) found" if findings else "No tracked secrets matched",
        findings=findings,
        metadata={"files_scanned": scanned, "max_file_bytes": MAX_SCAN_BYTES},
    )


def production_files(root: Path, configured: list[str]) -> list[Path]:
    files: set[Path] = set()
    for relative in configured:
        path = root / relative
        if path.is_file():
            files.add(path)
        elif path.is_dir():
            for candidate in path.rglob("*"):
                if candidate.is_file() and read_text(candidate) is not None:
                    files.add(candidate)
    return sorted(files)


def configuration_check(root: Path, policy: dict[str, Any]) -> Check:
    paths = policy.get("production_paths")
    if not isinstance(paths, list) or not paths or not all(isinstance(item, str) for item in paths):
        return Check("production-configuration", "failed", "Policy production_paths is missing or invalid")
    files = production_files(root, paths)
    if not files:
        return Check("production-configuration", "failed", "No production configuration files were found")

    findings: list[dict[str, Any]] = []
    dockerfiles: list[tuple[Path, str]] = []
    compose_files: list[tuple[Path, str]] = []
    for path in files:
        text = read_text(path)
        if text is None:
            continue
        relative = path.relative_to(root).as_posix()
        name = path.name.lower()
        if "dockerfile" in name:
            dockerfiles.append((path, text))
        if "compose" in name and path.suffix.lower() in {".yml", ".yaml"}:
            compose_files.append((path, text))
        for line_number, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith(("#", "//")):
                continue
            for rule, pattern in DANGEROUS_PATTERNS:
                if pattern.search(line):
                    findings.append({"rule": rule, "path": relative, "line": line_number})

    for path, text in dockerfiles:
        users = re.findall(r"(?im)^\s*USER\s+([^\s#]+)", text)
        if not users or users[-1].lower() in {"root", "0", "0:0"}:
            findings.append({"rule": "container-runs-as-root", "path": path.relative_to(root).as_posix(), "line": None})
    for path, text in compose_files:
        relative = path.relative_to(root).as_posix()
        if not re.search(r"(?im)^\s*read_only\s*:\s*true\s*$", text):
            findings.append({"rule": "compose-not-read-only", "path": relative, "line": None})
        if not re.search(r"(?im)^\s*-\s*[\"']?no-new-privileges(?::true)?[\"']?\s*$", text):
            findings.append({"rule": "compose-allows-privilege-escalation", "path": relative, "line": None})

    return Check(
        "production-configuration",
        "failed" if findings else "passed",
        f"{len(findings)} unsafe production setting(s) found" if findings else "Production defaults satisfy security policy",
        findings=findings,
        metadata={"files_scanned": [path.relative_to(root).as_posix() for path in files]},
    )


def parse_json_output(output: str) -> Any:
    try:
        return json.loads(output) if output.strip() else None
    except json.JSONDecodeError:
        return None


def dependency_scope(tracked: list[str], configured_paths: object) -> list[str]:
    if not isinstance(configured_paths, list) or not configured_paths:
        raise ValueError("security policy must declare non-empty dependency_paths")
    prefixes: list[str] = []
    for value in configured_paths:
        if not isinstance(value, str):
            raise TypeError("dependency_paths entries must be strings")
        normalized = value.replace("\\", "/").strip("/")
        if not normalized or normalized in {".", ".."} or ".." in normalized.split("/"):
            raise ValueError(f"unsafe dependency path: {value!r}")
        prefixes.append(normalized)
    return [
        relative
        for relative in tracked
        if any(relative == prefix or relative.startswith(f"{prefix}/") for prefix in prefixes)
    ]


def python_dependency_check(root: Path, tracked: list[str], audit_command: str) -> Check:
    manifests = sorted(
        item for item in tracked if Path(item).name.startswith("requirements") and Path(item).suffix == ".txt"
    )
    pyprojects = sorted(item for item in tracked if Path(item).name == "pyproject.toml")
    findings: list[dict[str, Any]] = []
    audited: list[str] = []
    try:
        import tomllib
    except ImportError:
        return Check("python-dependencies", "failed", "Python 3.11+ tomllib is required")

    audit_inputs: list[tuple[str, Path, bool]] = [
        (item, root / item, "--hash=" in (root / item).read_text(encoding="utf-8"))
        for item in manifests
    ]
    temporary_files: list[Path] = []
    try:
        for relative in pyprojects:
            project_directory = str(Path(relative).parent).replace("\\", "/")
            production_lock = f"{project_directory}/requirements-prod.txt"
            if production_lock in manifests:
                # CI proves the lock is regenerated from this declaration. Auditing the
                # complete hashed lock avoids platform-specific installation on the
                # Windows control runner while still checking every resolved package.
                continue
            try:
                data = tomllib.loads((root / relative).read_text(encoding="utf-8"))
                project = data.get("project", {})
                dependencies = list(project.get("dependencies", []))
                for values in project.get("optional-dependencies", {}).values():
                    dependencies.extend(values)
            except (OSError, ValueError, TypeError) as exc:
                findings.append({"manifest": relative, "error": f"cannot parse pyproject: {exc}"})
                continue
            if not dependencies:
                continue
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", suffix=".txt", delete=False
            ) as handle:
                handle.write("\n".join(str(item) for item in dependencies) + "\n")
                temp_path = Path(handle.name)
            temporary_files.append(temp_path)
            audit_inputs.append((relative, temp_path, False))

        if not audit_inputs and not findings:
            return Check("python-dependencies", "passed", "No auditable Python dependencies declared")
        for label, requirement, is_hashed_lock in audit_inputs:
            try:
                command = [
                    audit_command,
                    "--format",
                    "json",
                    "--progress-spinner",
                    "off",
                    "--requirement",
                    str(requirement),
                ]
                if is_hashed_lock:
                    command.append("--disable-pip")
                result = run(
                    command,
                    root,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                findings.append({"manifest": label, "error": f"pip-audit unavailable or timed out: {exc}"})
                continue
            audited.append(label)
            payload = parse_json_output(result.stdout)
            if result.returncode != 0:
                findings.append(
                    {
                        "manifest": label,
                        "error": "pip-audit reported vulnerabilities or could not complete",
                        "exit_code": result.returncode,
                        "details": payload,
                        "stderr": result.stderr.strip()[-2000:],
                    }
                )
    finally:
        for path in temporary_files:
            path.unlink(missing_ok=True)

    return Check(
        "python-dependencies",
        "failed" if findings else "passed",
        f"{len(findings)} Python audit failure(s)" if findings else "Python dependency audit passed",
        findings=findings,
        metadata={"audited_manifests": audited, "audit_command": audit_command},
    )


def npm_dependency_check(root: Path, tracked: list[str], npm_command: str, audit_level: str) -> Check:
    resolved_npm = shutil.which(npm_command) or npm_command
    manifests = sorted(item for item in tracked if Path(item).name == "package.json")
    findings: list[dict[str, Any]] = []
    audited: list[str] = []
    for relative in manifests:
        directory = (root / relative).parent
        try:
            manifest = json.loads((root / relative).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            findings.append({"manifest": relative, "error": f"cannot parse package.json: {exc}"})
            continue
        has_dependencies = any(manifest.get(key) for key in ("dependencies", "devDependencies", "optionalDependencies"))
        if not has_dependencies:
            continue
        lockfile = directory / "package-lock.json"
        if not lockfile.is_file():
            findings.append({"manifest": relative, "error": "package-lock.json is required for release auditing"})
            continue
        try:
            result = run(
                [resolved_npm, "audit", "--json", "--package-lock-only", "--ignore-scripts", f"--audit-level={audit_level}"],
                directory,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            findings.append({"manifest": relative, "error": f"npm audit unavailable or timed out: {exc}"})
            continue
        audited.append(relative)
        payload = parse_json_output(result.stdout)
        if result.returncode != 0:
            findings.append(
                {
                    "manifest": relative,
                    "error": "npm audit reported threshold vulnerabilities or could not complete",
                    "exit_code": result.returncode,
                    "details": payload,
                    "stderr": result.stderr.strip()[-2000:],
                }
            )
    return Check(
        "npm-dependencies",
        "failed" if findings else "passed",
        f"{len(findings)} npm audit failure(s)" if findings else "npm dependency audit passed",
        findings=findings,
        metadata={"audited_manifests": audited, "audit_level": audit_level, "audit_command": npm_command},
    )


def load_policy(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        policy = json.load(handle)
    if policy.get("version") != 1:
        raise ValueError("unsupported or missing policy version")
    return policy


def write_report(path: Path, root: Path | None, checks: list[Check], fatal_error: str | None = None) -> bool:
    failed = sum(check.status == "failed" for check in checks)
    skipped = sum(check.status == "skipped" for check in checks)
    payload = {
        "schema_version": 1,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "repository": str(root) if root else None,
        "release_ready": failed == 0 and skipped == 0 and fatal_error is None,
        "summary": {
            "passed": sum(check.status == "passed" for check in checks),
            "failed": failed,
            "skipped": skipped,
        },
        "fatal_error": fatal_error,
        "checks": [asdict(check) for check in checks],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return bool(payload["release_ready"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", default="deploy/production/security-policy.json")
    parser.add_argument("--report", default="artifacts/release/security-gate.json")
    parser.add_argument("--static-only", action="store_true", help="Skip networked dependency audits (not valid for a full release)")
    parser.add_argument("--pip-audit-command", default="pip-audit")
    parser.add_argument("--npm-command", default="npm")
    args = parser.parse_args()

    checks: list[Check] = []
    root: Path | None = None
    report = Path(args.report).resolve()
    try:
        root = repository_root()
        policy_path = Path(args.policy)
        if not policy_path.is_absolute():
            policy_path = root / policy_path
        policy = load_policy(policy_path)
        tracked, git_error = tracked_files(root)
        checks.append(secret_check(root, tracked, git_error))
        checks.append(configuration_check(root, policy))
        if args.static_only:
            checks.extend(
                [
                    Check("python-dependencies", "skipped", "Skipped by --static-only"),
                    Check("npm-dependencies", "skipped", "Skipped by --static-only"),
                ]
            )
        else:
            release_files = dependency_scope(tracked, policy.get("dependency_paths"))
            checks.append(
                python_dependency_check(root, release_files, args.pip_audit_command)
            )
            checks.append(
                npm_dependency_check(
                    root,
                    release_files,
                    args.npm_command,
                    str(policy.get("npm_audit_level", "high")),
                )
            )
    except Exception as exc:  # noqa: BLE001 - the report is the fail-closed contract.
        ready = write_report(report, root, checks, fatal_error=f"{type(exc).__name__}: {exc}")
        print(f"security gate: fatal error; report={report}", file=sys.stderr)
        return 0 if ready else 1

    ready = write_report(report, root, checks)
    for check in checks:
        print(f"[{check.status.upper():7}] {check.name}: {check.summary}")
    print(f"release_ready={str(ready).lower()} report={report}")
    return 0 if ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
