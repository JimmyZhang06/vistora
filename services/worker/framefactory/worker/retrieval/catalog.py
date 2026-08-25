"""Rights-aware retrieval from the existing local PostgreSQL asset catalog."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol

import psycopg
from psycopg.rows import dict_row

from framefactory.runtime import RetryableStepError

from .models import RetrievalCandidate, ScoreBreakdown


class RetrievalCatalog(Protocol):
    def search(
        self,
        workspace_id: str,
        library_ids: tuple[str, ...],
        query: str,
        limit: int,
        catalog_snapshot_id: str | None = None,
    ) -> Sequence[RetrievalCandidate]: ...


SearchCandidates = Callable[..., Sequence[RetrievalCandidate]]


class PostgresRetrievalCatalog:
    """Recall analyzed source windows from local, ready, rights-cleared media only."""

    def __init__(self, database_url: str) -> None:
        self.database_url = database_url

    def search(
        self,
        workspace_id: str,
        library_ids: tuple[str, ...],
        query: str,
        limit: int,
        catalog_snapshot_id: str | None = None,
    ) -> tuple[RetrievalCandidate, ...]:
        if not library_ids and catalog_snapshot_id is None:
            return ()
        if catalog_snapshot_id is None:
            asset_file_join = """JOIN LATERAL (
                             SELECT f.id,f.bucket,f.object_key,f.media_type,f.content_hash,
                                    f.byte_size,f.original_filename,f.duration_ms
                               FROM asset_files f
                              WHERE f.workspace_id=a.workspace_id AND f.asset_id=a.id
                                AND f.deleted_at IS NULL AND f.scan_status='clean'
                              ORDER BY f.created_at,f.id LIMIT 1
                           ) af ON true"""
            catalog_scope = """AND a.library_id=ANY(%s::uuid[])
                            AND aa.status='completed'
                            AND aa.analysis_version=(
                              SELECT max(current_aa.analysis_version)
                                FROM asset_analyses current_aa
                               WHERE current_aa.workspace_id=aa.workspace_id
                                 AND current_aa.asset_id=aa.asset_id
                                 AND current_aa.status='completed'
                            )"""
            scope_parameters: tuple[object, ...] = (list(library_ids),)
        else:
            # A Batch snapshot freezes both the immutable file bytes and the
            # exact analysis revision.  Never substitute the latest analysis
            # or fall back to the live library when this join is active.
            asset_file_join = """JOIN catalog_snapshot_items csi
                             ON csi.workspace_id=a.workspace_id
                            AND csi.library_id=a.library_id
                            AND csi.asset_id=a.id
                            AND csi.analysis_id=aa.id
                           JOIN asset_files af
                             ON af.workspace_id=csi.workspace_id
                            AND af.asset_id=csi.asset_id
                            AND af.id=csi.asset_file_id
                            AND af.content_hash=csi.content_hash
                            AND af.deleted_at IS NULL
                            AND af.scan_status='clean'"""
            library_scope = (
                "AND csi.library_id=ANY(%s::uuid[])" if library_ids else ""
            )
            catalog_scope = f"""AND csi.snapshot_id=%s::uuid
                            {library_scope}
                            AND aa.status IN ('completed','superseded')"""
            scope_parameters = (catalog_snapshot_id,)
            if library_ids:
                scope_parameters += (list(library_ids),)
        try:
            with psycopg.connect(self.database_url, row_factory=dict_row) as connection:
                rows = connection.execute(
                    f"""WITH recalled AS (
                         SELECT a.id AS asset_id,af.id AS asset_file_id,
                                aa.id AS analysis_id,s.id AS segment_id,
                                a.title,a.kind AS asset_kind,
                                af.bucket,af.object_key,af.media_type,
                                af.content_hash,af.byte_size,af.original_filename,
                                af.duration_ms AS source_duration_ms,
                                s.start_ms,s.end_ms,s.description,s.transcript,
                                s.cut_safe,s.semantic_complete,a.copyright_status,
                                s.people || s.locations || s.keywords ||
                                ARRAY_REMOVE(ARRAY[s.scene_type,s.action,s.era,s.mood,
                                                   s.visual_style,s.shot_type],NULL) ||
                                COALESCE(asset_labels.labels,ARRAY[]::text[]) AS labels,
                                similarity(s.search_text,%s)::double precision
                                  AS lexical_similarity,
                                label_hits.count::integer AS label_hits,
                                LEAST(0.64,label_hits.count * 0.32)::double precision
                                  AS label_score,
                                CASE WHEN s.cut_safe THEN 0.08 ELSE 0 END::double precision
                                  AS cut_safe_bonus,
                                CASE WHEN s.semantic_complete THEN 0.04 ELSE 0 END::double precision
                                  AS semantic_complete_bonus,
                                COALESCE(rights.evidence,'[]'::jsonb) AS rights_evidence
                           FROM asset_segments s
                           JOIN asset_analyses aa
                             ON aa.workspace_id=s.workspace_id AND aa.id=s.analysis_id
                           JOIN assets a
                             ON a.workspace_id=s.workspace_id AND a.id=s.asset_id
                           {asset_file_join}
                           LEFT JOIN LATERAL (
                             SELECT array_agg(t.name ORDER BY t.name) AS labels
                               FROM asset_tags at
                               JOIN tags t
                                 ON t.workspace_id=at.workspace_id AND t.id=at.tag_id
                              WHERE at.workspace_id=a.workspace_id AND at.asset_id=a.id
                           ) asset_labels ON true
                           LEFT JOIN LATERAL (
                             SELECT jsonb_agg(
                                      jsonb_build_object(
                                        'source_id',src.id,
                                        'source_type',src.source_type,
                                        'provider',src.provider,
                                        'locator',src.locator,
                                        'attribution',src.attribution,
                                        'license',src.license,
                                        'evidence_type',src.evidence_type,
                                        'verified_at',src.verified_at,
                                        'captured_at',src.captured_at,
                                        'metadata',src.metadata
                                      ) ORDER BY src.created_at,src.id
                                    ) AS evidence
                               FROM asset_sources src
                              WHERE src.workspace_id=a.workspace_id AND src.asset_id=a.id
                           ) rights ON true
                           CROSS JOIN LATERAL (
                             SELECT count(DISTINCT lower(label)) AS count
                               FROM unnest(
                                 s.people || s.locations || s.keywords ||
                                 ARRAY_REMOVE(ARRAY[s.scene_type,s.action,s.era,s.mood,
                                                    s.visual_style,s.shot_type],NULL) ||
                                 COALESCE(asset_labels.labels,ARRAY[]::text[])
                               ) label
                              WHERE char_length(label) >= 2
                                AND %s ILIKE '%%' || label || '%%'
                           ) label_hits
                          WHERE s.workspace_id=%s
                            {catalog_scope}
                            AND COALESCE(s.confidence,0) >= 0.5
                            AND (
                              -- A still has no media duration and receives its
                              -- display duration later in timeline.align.
                              a.kind='image'
                              OR (
                                a.kind='video'
                                AND af.duration_ms IS NOT NULL
                                AND s.end_ms <= af.duration_ms
                                AND s.end_ms-s.start_ms >= 3000
                                AND (
                                  s.start_ms >= 5000
                                  OR (
                                    s.start_ms=0 AND af.duration_ms <= 10000
                                    AND s.cut_safe AND s.semantic_complete
                                  )
                                )
                              )
                            )
                            AND a.kind IN ('image','video')
                            AND a.status='ready'
                            AND a.analysis_status='completed'
                            AND a.copyright_status IN ('owned','licensed','public_domain')
                            AND NOT (
                              lower(COALESCE(s.description,'')) ~
                                '(片尾|演职员|制作名单|credits?|credit roll)'
                              OR EXISTS (
                                SELECT 1 FROM unnest(s.keywords) keyword
                                 WHERE lower(keyword) IN
                                   ('片尾','演职员','制作名单','credits','credit roll')
                              )
                            )
                       )
                       SELECT * FROM recalled
                        ORDER BY (
                          lexical_similarity + label_score + cut_safe_bonus
                          + semantic_complete_bonus
                        ) DESC,
                        asset_id,asset_file_id,analysis_id,segment_id,start_ms,end_ms
                        LIMIT %s""",
                    (query, query, workspace_id, *scope_parameters, limit),
                ).fetchall()
        except psycopg.OperationalError as exc:
            raise RetryableStepError(
                "local asset catalog is temporarily unavailable",
                retry_after_seconds=15,
            ) from exc
        return tuple(_candidate_from_row(row) for row in rows)


def _candidate_from_row(row: Mapping[str, Any]) -> RetrievalCandidate:
    evidence = row.get("rights_evidence")
    if not isinstance(evidence, Sequence) or isinstance(evidence, (str, bytes)):
        evidence = ()
    return RetrievalCandidate(
        asset_id=str(row["asset_id"]),
        asset_file_id=str(row["asset_file_id"]),
        analysis_id=str(row["analysis_id"]),
        segment_id=str(row["segment_id"]),
        title=str(row["title"]),
        asset_kind=str(row["asset_kind"]),
        bucket=str(row["bucket"]),
        object_key=str(row["object_key"]),
        media_type=str(row["media_type"]),
        content_hash=str(row["content_hash"]),
        byte_size=int(row["byte_size"]),
        original_filename=str(row.get("original_filename") or "source.bin"),
        start_ms=int(row["start_ms"]),
        end_ms=int(row["end_ms"]),
        source_duration_ms=(
            int(row["source_duration_ms"])
            if row.get("source_duration_ms") is not None
            else None
        ),
        description=str(row["description"]),
        labels=tuple(str(item) for item in (row.get("labels") or ())),
        transcript=str(row.get("transcript") or ""),
        cut_safe=bool(row.get("cut_safe")),
        semantic_complete=bool(row.get("semantic_complete")),
        copyright_status=str(row.get("copyright_status") or "unknown"),
        rights_evidence=tuple(
            dict(item) for item in evidence if isinstance(item, Mapping)
        ),
        score_breakdown=ScoreBreakdown(
            lexical_similarity=float(row["lexical_similarity"]),
            label_hits=int(row["label_hits"]),
            label_score=float(row["label_score"]),
            cut_safe_bonus=float(row["cut_safe_bonus"]),
            semantic_complete_bonus=float(row["semantic_complete_bonus"]),
        ),
    )
