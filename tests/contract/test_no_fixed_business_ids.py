"""Guard the new contracts, migrations, and seed data against legacy business IDs."""

from __future__ import annotations

from pathlib import Path

from conftest import CONTRACTS_ROOT, MIGRATIONS_ROOT, SEEDS_ROOT

# Kept encoded so the guard does not match its own source if the scan scope grows.
FORBIDDEN_IDS = tuple(
    bytes.fromhex(value).decode("ascii")
    for value in (
        "62696f677261706879",
        "67656e65726963",
        "67656e7368696e",
        "626f6f6b6c697374",
        "61692d66726f6e74696572",
    )
)


def _scanned_files() -> list[Path]:
    allowed_suffixes = {".json", ".yaml", ".yml", ".sql", ".md", ".toml"}
    roots = (CONTRACTS_ROOT, MIGRATIONS_ROOT, SEEDS_ROOT)
    return sorted(path for root in roots if root.exists() for path in root.rglob("*") if path.suffix.lower() in allowed_suffixes)


def test_new_contracts_database_and_seeds_have_no_fixed_business_ids() -> None:
    violations: list[str] = []
    for path in _scanned_files():
        text = path.read_text(encoding="utf-8").lower()
        for forbidden_id in FORBIDDEN_IDS:
            relative_path = path.as_posix().lower()
            if forbidden_id in text or forbidden_id in relative_path:
                violations.append(f"{path}: {forbidden_id}")
    assert not violations, "legacy fixed business IDs leaked into the new architecture:\n" + "\n".join(violations)


def test_official_seed_format_contains_no_account_or_secret_data() -> None:
    seed_files = sorted(path for path in _scanned_files() if SEEDS_ROOT in path.parents)
    assert seed_files, "db/seeds must contain a minimal official seed format"
    combined = "\n".join(path.read_text(encoding="utf-8").lower() for path in seed_files)
    forbidden_account_tables = ("users", "sessions", "api_keys")
    for table in forbidden_account_tables:
        assert f"insert into {table}" not in combined
        assert f"insert into public.{table}" not in combined
    assert "password_hash" not in combined and "secret_hash" not in combined
