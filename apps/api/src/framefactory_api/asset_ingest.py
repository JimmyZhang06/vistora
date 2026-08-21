"""Durable v2 asset ingestion and versioned visual tagging CLI.

The importer intentionally accepts explicit source directories only. It never
reads legacy catalog JSON, generated Run directories, or previous tag files.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from .environment import environment_value
from .s3_storage import S3StorageSettings

_MEDIA_SUFFIXES = {
    ".bmp", ".jpeg", ".jpg", ".m4v", ".mkv", ".mov", ".mp4", ".png", ".webm", ".webp"
}
_VIDEO_SUFFIXES = {".m4v", ".mkv", ".mov", ".mp4", ".webm"}
_IGNORED_DIRECTORIES = {
    ".git",
    "_fin",
    "_judge",
    "_sheets",
    "_tag_demo",
    "_tagtmp",
    "artifacts",
    "exports",
    "runs",
}
_COPYRIGHT = {"unknown", "owned", "licensed", "public_domain", "restricted"}
_VISION_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
_PROMPT = """你是专业视频素材编目员。根据按时间顺序抽取的画面, 为素材生成可检索的客观标签。
不要根据文件名猜测, 不要判断版权归属, 不要把不确定的人物写成确定姓名。
只返回一个 JSON 对象, 结构如下:
{
  "summary":"客观画面摘要, 不超过80字",
  "language":"画面文字的主要语言或 unknown",
  "people":["明确可辨认的人物或人物类型"],
  "organizations":[], "locations":[], "eras":[],
  "scene_types":[], "actions":[], "moods":[], "visual_styles":[], "keywords":[],
  "has_embedded_text":false, "has_watermark":false,
  "safety":{"adult":false,"violence":false,"sensitive":false},
  "quality":{"usable":true,"score":0.0,"issues":[]},
  "confidence":0.0,
  "segments":[{
    "start_seconds":0.0,"end_seconds":1.0,"description":"片段内容",
    "people":[],"locations":[],"keywords":[],"scene_type":"","action":"",
    "era":"","mood":"","visual_style":"","shot_type":"","confidence":0.0
  }]
}
置信度与质量分数必须在 0 到 1 之间。片段必须按时间递增且不重叠。"""


@dataclass(frozen=True, slots=True)
class MediaProbe:
    kind: str
    media_type: str
    byte_size: int
    width: int | None
    height: int | None
    duration_ms: int | None


@dataclass(frozen=True, slots=True)
class ImportResult:
    discovered: int
    imported: int
    deduplicated: int
    tagged: int
    rejected: int


def project_root() -> Path:
    return Path(__file__).resolve().parents[4]


def validate_source_root(root: Path, *, repository_root: Path | None = None) -> Path:
    resolved = root.expanduser().resolve()
    if not resolved.is_dir() and not (
        resolved.is_file() and resolved.suffix.lower() in _MEDIA_SUFFIXES
    ):
        raise ValueError(f"asset source is not a supported file or directory: {resolved}")
    base = (repository_root or project_root()).resolve()
    forbidden = (base / "src" / "runs", base / "var" / "exports", base / "artifacts")
    contains_forbidden = any(
        resolved == path
        or path.is_relative_to(resolved)
        or resolved.is_relative_to(path)
        for path in forbidden
    )
    if resolved == base or contains_forbidden:
        raise ValueError("asset source is too broad or contains generated video output")
    return resolved


def discover_media(roots: Iterable[Path]) -> tuple[Path, ...]:
    files: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        candidates = (root,) if root.is_file() else root.rglob("*")
        for path in candidates:
            if not path.is_file() or path.suffix.lower() not in _MEDIA_SUFFIXES:
                continue
            relative_parts = () if root.is_file() else path.relative_to(root).parts
            if any(part.lower() in _IGNORED_DIRECTORIES for part in relative_parts):
                continue
            resolved = path.resolve()
            if resolved not in seen:
                seen.add(resolved)
                files.append(resolved)
    return tuple(sorted(files, key=lambda item: str(item).casefold()))


def normalize_analysis(value: Mapping[str, Any], *, duration_ms: int | None) -> dict[str, Any]:
    def text(name: str, maximum: int = 500) -> str:
        raw = value.get(name, "")
        return str(raw).strip()[:maximum] if raw is not None else ""

    def texts(name: str, maximum: int = 32) -> list[str]:
        raw = value.get(name, [])
        if not isinstance(raw, list):
            return []
        result: list[str] = []
        for item in raw:
            cleaned = str(item).strip()[:100]
            if cleaned and cleaned not in result:
                result.append(cleaned)
            if len(result) >= maximum:
                break
        return result

    normalized: dict[str, Any] = {
        "summary": text("summary"),
        "language": text("language", 32) or "unknown",
        "people": texts("people"),
        "organizations": texts("organizations"),
        "locations": texts("locations"),
        "eras": texts("eras"),
        "scene_types": texts("scene_types"),
        "actions": texts("actions"),
        "moods": texts("moods"),
        "visual_styles": texts("visual_styles"),
        "keywords": texts("keywords", 64),
        "has_embedded_text": value.get("has_embedded_text") is True,
        "has_watermark": value.get("has_watermark") is True,
        "safety": dict(value.get("safety", {})) if isinstance(value.get("safety"), Mapping) else {},
        "quality": (
            dict(value.get("quality", {}))
            if isinstance(value.get("quality"), Mapping)
            else {}
        ),
        "confidence": _confidence(value.get("confidence")),
        "segments": [],
    }
    maximum_end = duration_ms if duration_ms is not None else 86_400_000
    previous_end = 0
    raw_segments = value.get("segments", [])
    if isinstance(raw_segments, list):
        for ordinal, raw in enumerate(raw_segments[:64]):
            if not isinstance(raw, Mapping):
                continue
            start = max(previous_end, _milliseconds(raw.get("start_seconds")))
            end = min(maximum_end, _milliseconds(raw.get("end_seconds")))
            if end <= start:
                continue
            segment = {
                "ordinal": ordinal,
                "start_ms": start,
                "end_ms": end,
                "description": str(raw.get("description", "")).strip()[:500],
                "people": _text_list(raw.get("people")),
                "locations": _text_list(raw.get("locations")),
                "keywords": _text_list(raw.get("keywords"), maximum=32),
                "scene_type": _optional_text(raw.get("scene_type")),
                "action": _optional_text(raw.get("action")),
                "era": _optional_text(raw.get("era")),
                "mood": _optional_text(raw.get("mood")),
                "visual_style": _optional_text(raw.get("visual_style")),
                "shot_type": _optional_text(raw.get("shot_type")),
                "confidence": _confidence(raw.get("confidence")),
            }
            if segment["description"]:
                normalized["segments"].append(segment)
                previous_end = end
    return normalized


class DashScopeVisionTagger:
    def __init__(self, api_key: str, *, model: str = "qwen-vl-max", url: str = _VISION_URL) -> None:
        if not api_key.strip():
            raise ValueError("vision API key must not be empty")
        self.api_key = api_key.strip()
        self.model = model.strip()
        self.url = url

    def analyze(self, path: Path, probe: MediaProbe) -> dict[str, Any]:
        images = _sample_images(path, probe)
        content: list[dict[str, Any]] = [{"type": "text", "text": _PROMPT}]
        content.extend(
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{base64.b64encode(image).decode()}"},
            }
            for image in images
        )
        body = {
            "model": self.model,
            "messages": [{"role": "user", "content": content}],
            "temperature": 0.1,
            "max_tokens": 1800,
            "response_format": {"type": "json_object"},
        }
        response = _curl_json(self.url, self.api_key, body)
        try:
            raw = response["choices"][0]["message"]["content"]
            decoded = json.loads(raw) if isinstance(raw, str) else raw
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError("vision provider returned an invalid JSON response") from exc
        if not isinstance(decoded, Mapping):
            raise RuntimeError("vision provider response must contain a JSON object")
        return normalize_analysis(decoded, duration_ms=probe.duration_ms)


class AssetImporter:
    def __init__(self, connection: Any, s3_client: Any, settings: S3StorageSettings) -> None:
        self.connection = connection
        self.s3 = s3_client
        self.settings = settings

    async def run(
        self,
        *,
        roots: Sequence[Path],
        library_slug: str,
        library_name: str,
        copyright_status: str,
        tagger: DashScopeVisionTagger | None,
        limit: int | None,
    ) -> ImportResult:
        if copyright_status not in _COPYRIGHT:
            raise ValueError(f"unsupported copyright status: {copyright_status}")
        workspace_id, user_id = await self._identity()
        library_id = uuid5(NAMESPACE_URL, f"framefactory-library:{workspace_id}:{library_slug}")
        await self.connection.execute(
            """INSERT INTO asset_libraries
                   (id, workspace_id, name, slug, description, created_by)
               VALUES ($1,$2,$3,$4,$5,$6)
               ON CONFLICT (workspace_id, slug) DO UPDATE SET name=EXCLUDED.name,
                 description=EXCLUDED.description, updated_at=now()""",
            library_id,
            workspace_id,
            library_name,
            library_slug,
            "由新版内容指纹与视觉分析流水线管理的素材库",
            user_id,
        )
        files = discover_media(roots)
        if limit is not None:
            files = files[:limit]
        job_id = uuid5(
            NAMESPACE_URL,
            "framefactory-ingest:"
            + str(workspace_id)
            + ":"
            + "|".join(str(root) for root in roots),
        )
        await self.connection.execute(
            """INSERT INTO asset_ingestion_jobs
                   (id,workspace_id,library_id,source_root,status,discovered_count,
                    requested_by,started_at,updated_at)
               VALUES ($1,$2,$3,$4,'running',$5,$6,now(),now())
               ON CONFLICT (workspace_id,id) DO UPDATE SET status='running',
                 discovered_count=EXCLUDED.discovered_count,error=NULL,started_at=now(),
                 completed_at=NULL,updated_at=now()""",
            job_id,
            workspace_id,
            library_id,
            "|".join(str(root) for root in roots),
            len(files),
            user_id,
        )
        imported = deduplicated = tagged = rejected = 0
        errors: list[dict[str, str]] = []
        for path in files:
            try:
                probe = _probe(path)
                digest = _sha256(path)
                asset_id, created = await self._upsert_asset(
                    workspace_id=workspace_id,
                    user_id=user_id,
                    library_id=library_id,
                    path=path,
                    probe=probe,
                    digest=digest,
                    copyright_status=copyright_status,
                )
                if created:
                    imported += 1
                else:
                    deduplicated += 1
                if tagger is not None and not await self._has_completed_analysis(
                    workspace_id, asset_id
                ):
                    analysis = await asyncio.to_thread(tagger.analyze, path, probe)
                    await self._store_analysis(
                        workspace_id=workspace_id,
                        asset_id=asset_id,
                        provider="dashscope-openai-compatible",
                        model=tagger.model,
                        result=analysis,
                    )
                    tagged += 1
            except Exception as exc:  # continue a durable bulk import after one bad file
                rejected += 1
                errors.append({"path": str(path), "error": str(exc)[:500]})
            await self.connection.execute(
                """UPDATE asset_ingestion_jobs SET imported_count=$3,
                     deduplicated_count=$4,tagged_count=$5,rejected_count=$6,
                     error=$7::jsonb,updated_at=now() WHERE workspace_id=$1 AND id=$2""",
                workspace_id,
                job_id,
                imported,
                deduplicated,
                tagged,
                rejected,
                json.dumps({"files": errors[-100:]}) if errors else None,
            )
        final_status = "completed_with_errors" if errors else "completed"
        await self.connection.execute(
            """UPDATE asset_ingestion_jobs SET status=$3,completed_at=now(),updated_at=now()
                 WHERE workspace_id=$1 AND id=$2""",
            workspace_id,
            job_id,
            final_status,
        )
        return ImportResult(len(files), imported, deduplicated, tagged, rejected)

    async def _identity(self) -> tuple[UUID, UUID]:
        row = await self.connection.fetchrow(
            """SELECT w.id AS workspace_id, wm.user_id
                 FROM workspaces w JOIN workspace_members wm ON wm.workspace_id=w.id
                WHERE w.kind='personal' AND wm.status='active'
                ORDER BY w.created_at LIMIT 1"""
        )
        if row is None:
            raise RuntimeError("default workspace is missing; bootstrap the control API first")
        return row["workspace_id"], row["user_id"]

    async def _has_completed_analysis(self, workspace_id: UUID, asset_id: UUID) -> bool:
        return bool(
            await self.connection.fetchval(
                """SELECT EXISTS (
                     SELECT 1 FROM asset_analyses
                      WHERE workspace_id=$1 AND asset_id=$2 AND status='completed'
                   )""",
                workspace_id,
                asset_id,
            )
        )

    async def _upsert_asset(
        self,
        *,
        workspace_id: UUID,
        user_id: UUID,
        library_id: UUID,
        path: Path,
        probe: MediaProbe,
        digest: str,
        copyright_status: str,
    ) -> tuple[UUID, bool]:
        existing = await self.connection.fetchrow(
            """SELECT a.id FROM assets a JOIN asset_files f
                 ON f.workspace_id=a.workspace_id AND f.asset_id=a.id
                WHERE f.workspace_id=$1 AND f.content_hash=$2 AND f.deleted_at IS NULL""",
            workspace_id,
            digest,
        )
        if existing is not None:
            await self._upsert_source(workspace_id, existing["id"], path)
            return existing["id"], False
        asset_id = uuid5(NAMESPACE_URL, f"framefactory-asset:{workspace_id}:{digest}")
        file_id = uuid5(NAMESPACE_URL, f"framefactory-asset-file:{workspace_id}:{digest}")
        safe_name = _safe_filename(path.name)
        key = f"workspaces/{workspace_id}/assets/{digest}/{safe_name}"
        await asyncio.to_thread(self._upload, path, key, digest, probe.media_type)
        # Import never bypasses scanning, analysis and human review. Rights-safe media starts
        # processing; unknown or restricted media is quarantined.
        status = (
            "processing"
            if copyright_status in {"owned", "licensed", "public_domain"}
            else "quarantined"
        )
        async with self.connection.transaction():
            await self.connection.execute(
                """INSERT INTO assets
                     (id,workspace_id,library_id,kind,title,description,metadata,
                      copyright_status,status,created_by)
                   VALUES ($1,$2,$3,$4,$5,'',$6::jsonb,$7,$8,$9)""",
                asset_id,
                workspace_id,
                library_id,
                probe.kind,
                path.stem[:300],
                json.dumps({"ingestion_schema": "2.0.0"}),
                copyright_status,
                status,
                user_id,
            )
            await self.connection.execute(
                """INSERT INTO asset_files
                     (id,workspace_id,asset_id,storage_provider,bucket,object_key,
                      original_filename,media_type,byte_size,content_hash,scan_status,
                      width,height,duration_ms)
                   VALUES ($1,$2,$3,'s3',$4,$5,$6,$7,$8,$9,'pending',$10,$11,$12)""",
                file_id,
                workspace_id,
                asset_id,
                self.settings.bucket,
                key,
                path.name,
                probe.media_type,
                probe.byte_size,
                digest,
                probe.width,
                probe.height,
                probe.duration_ms,
            )
            await self._upsert_source(workspace_id, asset_id, path)
        return asset_id, True

    async def _upsert_source(self, workspace_id: UUID, asset_id: UUID, path: Path) -> None:
        locator = path.as_uri()
        source_id = uuid5(
            NAMESPACE_URL,
            f"framefactory-asset-source:{workspace_id}:{asset_id}:{locator}",
        )
        await self.connection.execute(
            """INSERT INTO asset_sources
                 (id,workspace_id,asset_id,source_type,locator,provider,metadata)
               VALUES ($1,$2,$3,'upload',$4,'filesystem-import',$5::jsonb)
               ON CONFLICT (workspace_id,id) DO NOTHING""",
            source_id,
            workspace_id,
            asset_id,
            locator,
            json.dumps({"original_path_hash": hashlib.sha256(str(path).encode()).hexdigest()}),
        )

    def _upload(self, path: Path, key: str, digest: str, media_type: str) -> None:
        try:
            existing = self.s3.head_object(Bucket=self.settings.bucket, Key=key)
        except Exception as exc:
            response = getattr(exc, "response", {})
            status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            code = response.get("Error", {}).get("Code")
            if status != 404 and code not in {"404", "NoSuchKey", "NotFound"}:
                raise
        else:
            if (
                int(existing.get("ContentLength", -1)) != path.stat().st_size
                or existing.get("Metadata", {}).get("sha256") != digest
            ):
                raise RuntimeError("existing object does not match its content-addressed key")
            return
        from boto3.s3.transfer import TransferConfig

        self.s3.upload_file(
            str(path),
            self.settings.bucket,
            key,
            ExtraArgs={"ContentType": media_type, "Metadata": {"sha256": digest}},
            Config=TransferConfig(
                multipart_threshold=64 * 1024 * 1024,
                multipart_chunksize=64 * 1024 * 1024,
                max_concurrency=4,
                use_threads=True,
            ),
        )
        stored = self.s3.head_object(Bucket=self.settings.bucket, Key=key)
        if (
            int(stored.get("ContentLength", -1)) != path.stat().st_size
            or stored.get("Metadata", {}).get("sha256") != digest
        ):
            raise RuntimeError("multipart upload failed integrity verification")

    async def _store_analysis(
        self,
        *,
        workspace_id: UUID,
        asset_id: UUID,
        provider: str,
        model: str,
        result: Mapping[str, Any],
    ) -> None:
        version = int(
            await self.connection.fetchval(
                """SELECT COALESCE(max(analysis_version),0)+1 FROM asset_analyses
                    WHERE workspace_id=$1 AND asset_id=$2""",
                workspace_id,
                asset_id,
            )
        )
        analysis_id = uuid5(
            NAMESPACE_URL,
            f"framefactory-analysis:{workspace_id}:{asset_id}:{version}:{provider}:{model}",
        )
        search_parts = [
            str(result.get("summary", "")),
            *[item for name in _ANALYSIS_ARRAYS for item in result.get(name, [])],
        ]
        async with self.connection.transaction():
            await self.connection.execute(
                """UPDATE asset_analyses SET status='superseded'
                    WHERE workspace_id=$1 AND asset_id=$2 AND status='completed'""",
                workspace_id,
                asset_id,
            )
            await self.connection.execute(
                """INSERT INTO asset_analyses
                   (id,workspace_id,asset_id,analysis_version,provider,model,summary,language,
                    people,organizations,locations,eras,scene_types,actions,moods,visual_styles,
                    keywords,has_embedded_text,has_watermark,safety,quality,confidence,search_text,
                    raw_result)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,
                           $19,$20::jsonb,$21::jsonb,$22,$23,$24::jsonb)""",
                analysis_id,
                workspace_id,
                asset_id,
                version,
                provider,
                model,
                result.get("summary", ""),
                result.get("language"),
                result.get("people", []),
                result.get("organizations", []),
                result.get("locations", []),
                result.get("eras", []),
                result.get("scene_types", []),
                result.get("actions", []),
                result.get("moods", []),
                result.get("visual_styles", []),
                result.get("keywords", []),
                result.get("has_embedded_text", False),
                result.get("has_watermark", False),
                json.dumps(result.get("safety", {}), ensure_ascii=False),
                json.dumps(result.get("quality", {}), ensure_ascii=False),
                result.get("confidence"),
                " ".join(str(part) for part in search_parts if part).strip(),
                json.dumps(dict(result), ensure_ascii=False),
            )
            await self.connection.execute(
                """UPDATE assets SET analysis_status='completed',revision=revision+1,
                     updated_at=now() WHERE workspace_id=$1 AND id=$2""",
                workspace_id,
                asset_id,
            )
            for segment in result.get("segments", []):
                segment_id = uuid5(
                    NAMESPACE_URL,
                    f"framefactory-segment:{analysis_id}:{segment['ordinal']}",
                )
                segment_search = " ".join(
                    str(item)
                    for item in (
                        segment.get("description", ""),
                        *segment.get("people", []),
                        *segment.get("locations", []),
                        *segment.get("keywords", []),
                        segment.get("scene_type", ""),
                        segment.get("action", ""),
                    )
                    if item
                )
                await self.connection.execute(
                    """INSERT INTO asset_segments
                       (id,workspace_id,asset_id,analysis_id,ordinal,start_ms,end_ms,description,
                        people,locations,keywords,scene_type,action,era,mood,visual_style,shot_type,
                        confidence,search_text)
                       VALUES
                         ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19)""",
                    segment_id,
                    workspace_id,
                    asset_id,
                    analysis_id,
                    segment["ordinal"],
                    segment["start_ms"],
                    segment["end_ms"],
                    segment["description"],
                    segment["people"],
                    segment["locations"],
                    segment["keywords"],
                    segment["scene_type"],
                    segment["action"],
                    segment["era"],
                    segment["mood"],
                    segment["visual_style"],
                    segment["shot_type"],
                    segment["confidence"],
                    segment_search,
                )
            for tag_name in _analysis_tags(result):
                tag_id = uuid5(
                    NAMESPACE_URL,
                    f"framefactory-tag:{workspace_id}:{tag_name.casefold()}",
                )
                slug = "ai-" + hashlib.sha256(tag_name.casefold().encode()).hexdigest()[:24]
                await self.connection.execute(
                    """INSERT INTO tags (id,workspace_id,name,slug) VALUES ($1,$2,$3,$4)
                       ON CONFLICT (workspace_id,slug) DO UPDATE SET name=EXCLUDED.name""",
                    tag_id,
                    workspace_id,
                    tag_name,
                    slug,
                )
                await self.connection.execute(
                    """INSERT INTO asset_tags
                       (workspace_id,asset_id,tag_id,analysis_id,confidence,source)
                       VALUES ($1,$2,$3,$4,$5,'vision')
                       ON CONFLICT (workspace_id,asset_id,tag_id) DO UPDATE SET
                         analysis_id=EXCLUDED.analysis_id,confidence=EXCLUDED.confidence,
                         source=EXCLUDED.source,created_at=now()""",
                    workspace_id,
                    asset_id,
                    tag_id,
                    analysis_id,
                    result.get("confidence"),
                )


_ANALYSIS_ARRAYS = (
    "people",
    "organizations",
    "locations",
    "eras",
    "scene_types",
    "actions",
    "moods",
    "visual_styles",
    "keywords",
)


def _analysis_tags(result: Mapping[str, Any]) -> tuple[str, ...]:
    values: list[str] = []
    for name in _ANALYSIS_ARRAYS:
        raw = result.get(name, [])
        if isinstance(raw, list):
            values.extend(str(item).strip() for item in raw if str(item).strip())
    return tuple(dict.fromkeys(values))[:128]


def _confidence(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return round(max(0.0, min(1.0, number)), 3)


def _milliseconds(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, round(float(value) * 1000))
    except (TypeError, ValueError):
        return 0


def _optional_text(value: Any) -> str | None:
    cleaned = str(value).strip()[:100] if value is not None else ""
    return cleaned or None


def _text_list(value: Any, *, maximum: int = 16) -> list[str]:
    if not isinstance(value, list):
        return []
    items = dict.fromkeys(str(item).strip()[:100] for item in value if str(item).strip())
    return list(items)[:maximum]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _safe_filename(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-")
    suffix = Path(value).suffix.lower()
    if not cleaned or (suffix and not cleaned.lower().endswith(suffix)):
        cleaned = f"source-{hashlib.sha256(value.encode()).hexdigest()[:12]}{suffix}"
    return cleaned[:180]


def _probe(path: Path) -> MediaProbe:
    if shutil.which("ffprobe") is None:
        raise RuntimeError("ffprobe is required for asset validation")
    process = subprocess.run(
        (
            "ffprobe",
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(path),
        ),
        capture_output=True,
        check=False,
        timeout=120,
    )
    if process.returncode != 0:
        raise ValueError("ffprobe rejected the media file")
    try:
        data = json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError("ffprobe returned invalid metadata") from exc
    streams = data.get("streams", [])
    visual = next((item for item in streams if item.get("codec_type") == "video"), {})
    duration = data.get("format", {}).get("duration") or visual.get("duration")
    duration_ms = round(float(duration) * 1000) if duration else None
    kind = "video" if path.suffix.lower() in _VIDEO_SUFFIXES else "image"
    media_type = (
        ("video/x-matroska" if path.suffix.lower() == ".mkv" else None)
        or mimetypes.guess_type(path.name)[0]
        or ("video/mp4" if kind == "video" else "image/jpeg")
    )
    return MediaProbe(
        kind=kind,
        media_type=media_type,
        byte_size=path.stat().st_size,
        width=int(visual["width"]) if visual.get("width") else None,
        height=int(visual["height"]) if visual.get("height") else None,
        duration_ms=duration_ms,
    )


def _sample_images(path: Path, probe: MediaProbe) -> tuple[bytes, ...]:
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg is required for visual tagging")
    count = 5 if probe.kind == "video" else 1
    duration = (probe.duration_ms or 1000) / 1000
    times = [duration * (0.05 + 0.9 * index / max(1, count - 1)) for index in range(count)]
    images: list[bytes] = []
    with tempfile.TemporaryDirectory(prefix="framefactory-asset-frames-") as directory:
        for index, timestamp in enumerate(times):
            output = Path(directory) / f"frame-{index}.jpg"
            command = ["ffmpeg", "-y", "-v", "error"]
            if probe.kind == "video":
                command.extend(("-ss", f"{timestamp:.3f}"))
            command.extend(("-i", str(path), "-frames:v", "1", "-vf", "scale=640:-2", str(output)))
            result = subprocess.run(command, capture_output=True, check=False, timeout=120)
            if result.returncode != 0 or not output.is_file():
                raise RuntimeError("ffmpeg could not sample the asset")
            images.append(output.read_bytes())
    return tuple(images)


def _curl_json(url: str, api_key: str, body: Mapping[str, Any]) -> Mapping[str, Any]:
    if shutil.which("curl") is None:
        raise RuntimeError("curl is required for the configured vision provider")
    with tempfile.TemporaryDirectory(prefix="framefactory-vision-") as directory:
        body_path = Path(directory) / "request.json"
        header_path = Path(directory) / "headers.txt"
        body_path.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
        header_path.write_text(
            f"Authorization: Bearer {api_key}\nContent-Type: application/json\n",
            encoding="utf-8",
        )
        process = subprocess.run(
            (
                "curl",
                "-sS",
                "--ssl-no-revoke",
                "--retry",
                "2",
                "--max-time",
                "240",
                "-X",
                "POST",
                url,
                "--header",
                f"@{header_path}",
                "--data-binary",
                f"@{body_path}",
            ),
            capture_output=True,
            check=False,
            timeout=300,
        )
    if process.returncode != 0:
        raise RuntimeError("vision provider request failed")
    try:
        result = json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("vision provider returned non-JSON data") from exc
    if not isinstance(result, Mapping) or "error" in result:
        raise RuntimeError("vision provider rejected the tagging request")
    return result


def _s3_client(settings: S3StorageSettings) -> Any:
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=settings.endpoint_url,
        region_name=settings.region,
        aws_access_key_id=settings.access_key_id,
        aws_secret_access_key=settings.secret_access_key,
        config=Config(signature_version="s3v4", s3={"addressing_style": settings.addressing_style}),
        verify=settings.verify_tls,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="framefactory-assets")
    parser.add_argument("--source", action="append", required=True, type=Path)
    parser.add_argument("--library-slug", default="reindexed-source-media")
    parser.add_argument("--library-name", default="重新编目的源素材")
    parser.add_argument("--copyright-status", choices=sorted(_COPYRIGHT), default="unknown")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--tag", action="store_true")
    parser.add_argument("--vision-model", default="qwen-vl-max")
    parser.add_argument("--vision-url", default=_VISION_URL)
    return parser


async def _run(arguments: argparse.Namespace) -> ImportResult:
    try:
        import asyncpg
    except ImportError as exc:  # pragma: no cover - packaging error
        raise RuntimeError("asset ingestion requires asyncpg") from exc
    database_url = os.getenv("FRAMEFACTORY_DATABASE_URL", "").strip()
    if not database_url:
        raise ValueError("FRAMEFACTORY_DATABASE_URL is required")
    roots = tuple(validate_source_root(path) for path in arguments.source)
    settings = S3StorageSettings.from_environment()
    tagger = None
    if arguments.tag:
        api_key = environment_value("FRAMEFACTORY_VISION_API_KEY") or environment_value(
            "DASHSCOPE_API_KEY"
        )
        tagger = DashScopeVisionTagger(
            api_key or "", model=arguments.vision_model, url=arguments.vision_url
        )
    connection = await asyncpg.connect(database_url)
    try:
        importer = AssetImporter(connection, _s3_client(settings), settings)
        return await importer.run(
            roots=roots,
            library_slug=arguments.library_slug,
            library_name=arguments.library_name,
            copyright_status=arguments.copyright_status,
            tagger=tagger,
            limit=arguments.limit,
        )
    finally:
        await connection.close()


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.limit is not None and arguments.limit < 1:
        raise SystemExit("--limit must be positive")
    result = asyncio.run(_run(arguments))
    print(json.dumps(asdict(result), ensure_ascii=False, sort_keys=True))
    return 0 if result.rejected == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
