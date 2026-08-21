from __future__ import annotations

import inspect
import unittest
from pathlib import Path

from framefactory.worker.adapters.postgres_asset_jobs import PostgresAssetJobRepository

ROOT = Path(__file__).resolve().parents[3]
MIGRATION = ROOT / "db" / "migrations" / "0010_asset_analysis_pipeline.sql"
TEMPORAL_MIGRATION = ROOT / "db" / "migrations" / "0011_asset_temporal_intelligence.sql"


class AssetJobPersistenceContractTests(unittest.TestCase):
    def test_migration_has_item_level_recovery_and_review_audit(self) -> None:
        sql = MIGRATION.read_text(encoding="utf-8")
        self.assertIn("'awaiting_review'", sql)
        self.assertIn("CREATE TABLE asset_analysis_batches", sql)
        self.assertIn("CREATE TABLE asset_analysis_items", sql)
        self.assertIn("completed_stages text[]", sql)
        self.assertIn("checkpoints jsonb", sql)
        self.assertIn("lease_expires_at timestamptz", sql)
        self.assertIn("failure_reason text", sql)
        self.assertIn("CREATE TABLE asset_review_actions", sql)
        self.assertIn("requested_limit BETWEEN 1 AND 100", sql)

    def test_publish_auto_ready_is_explicit_and_approval_checks_rights(self) -> None:
        source = inspect.getsource(PostgresAssetJobRepository)
        self.assertIn('"auto_ready": False', source)
        self.assertIn("gate.eligible_for_auto_ready", source)
        self.assertIn('target_status = "ready" if auto_ready else "awaiting_review"', source)
        self.assertIn("copyright must be resolved before approval", source)
        self.assertIn("scan and completed analysis are required before approval", source)
        self.assertIn("FOR UPDATE OF i,a", source)
        self.assertIn('metadata_patch["preview"]', source)

    def test_claim_and_checkpoint_use_leases_and_revisions(self) -> None:
        source = inspect.getsource(PostgresAssetJobRepository)
        self.assertIn("SKIP LOCKED", source)
        self.assertIn("lease_expires_at < now()", source)
        self.assertIn("revision=%s", source)
        self.assertIn("status='retry_wait'", source)

    def test_temporal_migration_persists_transcripts_and_cut_evidence(self) -> None:
        sql = TEMPORAL_MIGRATION.read_text(encoding="utf-8")
        self.assertIn("CREATE TABLE IF NOT EXISTS asset_transcripts", sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS asset_transcript_cues", sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS asset_shot_boundaries", sql)
        self.assertIn("ADD COLUMN IF NOT EXISTS transcript", sql)
        self.assertIn("ADD COLUMN IF NOT EXISTS cut_safe", sql)
        self.assertIn("ADD COLUMN IF NOT EXISTS semantic_complete", sql)


if __name__ == "__main__":
    unittest.main()
