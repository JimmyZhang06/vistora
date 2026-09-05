# ruff: noqa: RUF001 - Chinese punctuation is intentional in user-facing report copy.

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class BenchmarkMediaModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BenchmarkMediaSegment(BenchmarkMediaModel):
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)
    description: str = Field(min_length=1, max_length=500)
    shot_type: str | None = Field(default=None, max_length=80)
    camera_motion: str | None = Field(default=None, max_length=80)
    visual_style: str | None = Field(default=None, max_length=120)
    transcript: str = Field(default="", max_length=4000)
    ocr_text: list[str] = Field(default_factory=list, max_length=32)
    audio_events: list[str] = Field(default_factory=list, max_length=32)
    confidence: float | None = Field(default=None, ge=0, le=1)
    representative_frame_key: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def validate_range(self) -> BenchmarkMediaSegment:
        if self.end_ms <= self.start_ms:
            raise ValueError("segment end_ms must be greater than start_ms")
        return self


class BenchmarkMediaEvidence(BenchmarkMediaModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    source_kind: Literal["worker_asset_analysis", "synthetic_demo"]
    source_label: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=200)
    duration_ms: int = Field(gt=0, le=3_600_000)
    width: int | None = Field(default=None, gt=0, le=16_384)
    height: int | None = Field(default=None, gt=0, le=16_384)
    has_audio: bool
    speech_status: Literal["present", "absent", "unknown"] = "unknown"
    speech_detection_method: str | None = Field(default=None, max_length=200)
    analysis_status: Literal["complete", "partial"] | None = None
    sampled_frames: bool = False
    transcript_text: str | None = Field(default=None, max_length=100_000)
    speech_duration_ms: int | None = Field(default=None, gt=0, le=3_600_000)
    segments: list[BenchmarkMediaSegment] = Field(min_length=1, max_length=2000)
    silences: list[tuple[int, int]] = Field(default_factory=list, max_length=2000)
    limitations: list[str] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def validate_timeline(self) -> BenchmarkMediaEvidence:
        previous_end = 0
        for segment in self.segments:
            if segment.start_ms < previous_end:
                raise ValueError("segments must be ordered and non-overlapping")
            if segment.end_ms > self.duration_ms:
                raise ValueError("segment end_ms exceeds media duration")
            previous_end = segment.end_ms
        for start_ms, end_ms in self.silences:
            if start_ms < 0 or end_ms <= start_ms or end_ms > self.duration_ms:
                raise ValueError("silence range is outside media duration")
        return self


class BenchmarkDeepMetric(BenchmarkMediaModel):
    key: str
    label: str
    value: str
    interpretation: str


class BenchmarkDeepFinding(BenchmarkMediaModel):
    category: Literal["hook", "visual", "narration", "rhythm", "limitation"]
    confidence: Literal["high", "medium", "low"]
    claim: str
    evidence: list[str]
    reusable_move: str | None = None


class BenchmarkDeepTimelineItem(BenchmarkMediaModel):
    start_ms: int
    end_ms: int
    label: str
    description: str
    transcript: str
    ocr_text: list[str]
    audio_events: list[str]
    evidence_types: list[Literal["frame", "ocr", "asr", "audio"]]
    confidence: float | None = None
    representative_frame_key: str | None = None


class BenchmarkDeepNoteReport(BenchmarkMediaModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    source_kind: Literal["worker_asset_analysis", "synthetic_demo"]
    source_label: str
    evidence_depth: Literal["multimodal_timeline_v1"] = "multimodal_timeline_v1"
    title: str
    duration_ms: int
    status: Literal["ready", "partial"]
    summary: str
    metrics: list[BenchmarkDeepMetric]
    findings: list[BenchmarkDeepFinding]
    timeline: list[BenchmarkDeepTimelineItem]
    limitations: list[str]


_QUESTION = re.compile(r"[?？]|^(?:你|大家|有没有|是不是|怎么|为什么)")
_METHOD = re.compile(r"(?:这样|这么|教程|方法|步骤|拍法|学会|试试|教你)")
_FIRST_PERSON = re.compile(r"(?:我|我们|我的)")


def build_benchmark_deep_note_report(
    evidence: BenchmarkMediaEvidence,
) -> BenchmarkDeepNoteReport:
    duration_seconds = evidence.duration_ms / 1000
    shot_count = len(evidence.segments)
    average_shot_ms = round(evidence.duration_ms / shot_count)
    transcript = (
        evidence.transcript_text
        if evidence.transcript_text is not None
        else "".join(segment.transcript.strip() for segment in evidence.segments)
    )
    transcript_characters = len(re.sub(r"\s+", "", transcript))
    speech_minutes = max((evidence.speech_duration_ms or evidence.duration_ms) / 60_000, 1 / 60)
    speech_rate = round(transcript_characters / speech_minutes) if transcript_characters else 0
    silence_ms = _union_duration(evidence.silences)
    silence_percent = round(silence_ms / evidence.duration_ms * 100, 1)
    overlay_segments = sum(bool(segment.ocr_text) for segment in evidence.segments)
    first_three_seconds = [segment for segment in evidence.segments if segment.start_ms < 3000]
    audio_event_count = sum(len(segment.audio_events) for segment in evidence.segments)

    metrics = [
        BenchmarkDeepMetric(
            key="shot_pace",
            label="证据采样节奏" if evidence.sampled_frames else "镜头节奏",
            value=f"{shot_count} 段 · 均长 {average_shot_ms / 1000:.1f}s",
            interpretation=(
                "这是代表帧覆盖区间，不等同于真实剪辑镜头数。"
                if evidence.sampled_frames
                else _pace_interpretation(average_shot_ms)
            ),
        ),
        BenchmarkDeepMetric(
            key="hook_density",
            label="前三秒信息密度",
            value=(
                f"{len(first_three_seconds)} 段 · "
                f"{sum(len(item.ocr_text) for item in first_three_seconds)} 组字幕"
            ),
            interpretation="同时计算画面变化与屏幕文字，不把标题信号重复计数。",
        ),
        BenchmarkDeepMetric(
            key="speech_rate",
            label="语音字速" if evidence.speech_duration_ms else "全片转写字密度",
            value=(
                f"{speech_rate} 字/分钟"
                if transcript_characters
                else "未检测到口播"
                if evidence.speech_status == "absent"
                else "未获得口播文本"
            ),
            interpretation=(
                (
                    "按带时间码语音片段时长计算。"
                    if evidence.speech_duration_ms
                    else "按全片时长计算的转写字密度，不等同于真实口播速度。"
                )
                if transcript_characters
                else "语音检测明确返回无语音段；不虚构口播文案或语速。"
                if evidence.speech_status == "absent"
                else "ASR 缺失时不推断语速。"
            ),
        ),
        BenchmarkDeepMetric(
            key="silence_ratio",
            label="停顿占比",
            value=f"{silence_percent}%" if evidence.has_audio else "无音轨",
            interpretation="由音频静音区间计算；不等同于没有背景音乐。",
        ),
    ]

    findings: list[BenchmarkDeepFinding] = []
    if first_three_seconds:
        evidence_lines = [
            f"{_time_range(item.start_ms, item.end_ms)}：{item.description}"
            for item in first_three_seconds[:3]
        ]
        if any(item.ocr_text for item in first_three_seconds):
            evidence_lines.append(
                "前三秒屏幕文字："
                + " / ".join(text for item in first_three_seconds for text in item.ocr_text)[:180]
            )
        findings.append(
            BenchmarkDeepFinding(
                category="hook",
                confidence="low",
                claim="开场候选吸引点可从以下画面和文字检查；是否构成有效钩子仍是策略假设。",
                evidence=evidence_lines,
                reusable_move="把冲突、结果或明确收益放入第一镜，并在三秒内用字幕完成语义确认。",
            )
        )

    visual_descriptions = {segment.description for segment in evidence.segments}
    if len(visual_descriptions) >= 3:
        findings.append(
            BenchmarkDeepFinding(
                category="visual",
                confidence="medium",
                claim="多个采样段呈现不同的画面描述；信息是否递进需结合时间线复核。",
                evidence=[
                    f"全片识别 {len(visual_descriptions)} 个不同的画面描述。",
                    f"平均证据区间长度 {average_shot_ms / 1000:.1f} 秒，不代表精确镜头长度。",
                ],
                reusable_move="按“问题画面—动作过程—结果揭晓”组织镜头，每一镜只承担一个信息任务。",
            )
        )
    if overlay_segments:
        findings.append(
            BenchmarkDeepFinding(
                category="visual",
                confidence="high",
                claim="部分采样段识别到屏幕文字；其叙事功能需对照文字内容和画面判断。",
                evidence=[f"{overlay_segments}/{shot_count} 个时间段检测到 OCR 文字。"],
                reusable_move="字幕只补充画面无法独立表达的信息，并保持短句和稳定位置。",
            )
        )

    if transcript_characters:
        opening_transcript = "".join(item.transcript for item in first_three_seconds).strip()
        devices = []
        if _QUESTION.search(opening_transcript):
            devices.append("问题句")
        if _METHOD.search(transcript):
            devices.append("方法承诺")
        if _FIRST_PERSON.search(transcript):
            devices.append("第一人称代入")
        findings.append(
            BenchmarkDeepFinding(
                category="narration",
                confidence="low",
                claim="ASR 获得未分类的语音候选，尚不能确认是口播、唱词或背景人声。",
                evidence=[
                    f"ASR 获得 {transcript_characters} 个非空白字符，约 {speech_rate} 字/分钟。",
                    f"转写中的词句线索：{'、'.join(devices) if devices else '未识别到明确线索'}。",
                    f"开场转写候选：{opening_transcript[:120] or '前三秒无可用转写'}",
                ],
                reusable_move="先回听确认人声来源与转写准确性，再判断是否适合复用为口播结构。",
            )
        )
    elif evidence.speech_status == "absent":
        findings.append(
            BenchmarkDeepFinding(
                category="narration",
                confidence="high" if evidence.speech_detection_method else "medium",
                claim="当前语音检测未发现语音活动，暂不分析口播文案；音乐或较轻语音可能影响检测。",
                evidence=[
                    "语音检测没有获得任何可用语音段。",
                    (
                        f"检测方法：{evidence.speech_detection_method}。"
                        if evidence.speech_detection_method
                        else "上游明确标记为无口播。"
                    ),
                ],
                reusable_move="氛围型内容可省略口播，但首镜文字、动作转场和色彩母题必须共同承担叙事。",
            )
        )

    if audio_event_count or silence_ms:
        findings.append(
            BenchmarkDeepFinding(
                category="rhythm",
                confidence="medium",
                claim="音轨中检测到语音活动或低能量区间；与剪辑节奏的关系需要对齐复核。",
                evidence=[
                    f"记录 {audio_event_count} 个声音事件。",
                    f"静音区间占视频时长 {silence_percent}%。",
                ],
                reusable_move="把转场、动作落点或结果揭晓对齐到重音，并给关键信息保留短暂停顿。",
            )
        )

    limitations = list(evidence.limitations)
    if transcript_characters:
        limitations.append("ASR 文本未区分口播、唱词和背景人声，字速及词句线索不证明口播策略。")
    if not transcript_characters and evidence.speech_status == "unknown":
        limitations.append("当前证据没有可用 ASR 文本，因此不评价口播文案和真实语速。")
    if not overlay_segments and evidence.analysis_status != "complete":
        limitations.append("当前证据没有 OCR 文字，因此不评价字幕布局和信息层级。")
    limitations.append("没有曝光、完播率或对照实验；不能据此证明推荐机制或爆款因果。")
    if evidence.source_kind == "synthetic_demo":
        limitations.insert(
            0,
            "这是系统验证样片的结构化证据，仅用于展示分析能力，不是目标博主的真实视频结论。",
        )

    timeline = [_timeline_item(segment, index) for index, segment in enumerate(evidence.segments)]
    speech_resolved = bool(transcript_characters) or evidence.speech_status == "absent"
    status: Literal["ready", "partial"] = (
        ("ready" if evidence.analysis_status == "complete" else "partial")
        if evidence.analysis_status is not None
        else "ready"
        if speech_resolved and overlay_segments
        else "partial"
    )
    summary = (
        f"这段 {duration_seconds:.1f} 秒素材被拆为 {shot_count} 个证据时间段。"
        f"前三秒包含 {len(first_three_seconds)} 个画面段，"
        f"平均证据区间长度 {average_shot_ms / 1000:.1f} 秒；"
        + (
            f"转写约 {speech_rate} 字/分钟，OCR 覆盖 {overlay_segments}/{shot_count} 个时间段。"
            if transcript_characters
            else (
                f"OCR 覆盖 {overlay_segments}/{shot_count} 个时间段，语音检测未发现口播。"
                if evidence.speech_status == "absent"
                else f"OCR 覆盖 {overlay_segments}/{shot_count} 个时间段，口播证据尚未获得。"
            )
        )
    )
    return BenchmarkDeepNoteReport(
        source_kind=evidence.source_kind,
        source_label=evidence.source_label,
        title=evidence.title,
        duration_ms=evidence.duration_ms,
        status=status,
        summary=summary,
        metrics=metrics,
        findings=findings,
        timeline=timeline,
        limitations=list(dict.fromkeys(limitations)),
    )


def benchmark_deep_report_demo() -> BenchmarkDeepNoteReport:
    return build_benchmark_deep_note_report(
        BenchmarkMediaEvidence(
            source_kind="synthetic_demo",
            source_label="系统验证样片 · 竖屏转场教程",
            title="三秒转场教程 · 多模态分析结构样例",
            duration_ms=12_400,
            width=1080,
            height=1920,
            has_audio=True,
            silences=[(2280, 2600), (7800, 8240)],
            limitations=["未接入真实曝光、完播、分享和评论数据。"],
            segments=[
                BenchmarkMediaSegment(
                    start_ms=0,
                    end_ms=1380,
                    description="人物近景举起相机，画面快速推近",
                    shot_type="近景",
                    camera_motion="快速推近",
                    visual_style="高亮暖色",
                    transcript="假期不知道怎么拍？",
                    ocr_text=["假期这样拍"],
                    audio_events=["音乐重拍", "口播进入"],
                    confidence=0.93,
                ),
                BenchmarkMediaSegment(
                    start_ms=1380,
                    end_ms=3120,
                    description="手掌遮挡镜头形成匹配转场",
                    shot_type="特写",
                    camera_motion="遮挡转场",
                    visual_style="动作匹配",
                    transcript="试试这个超简单的遮挡转场。",
                    ocr_text=["第一步：遮住镜头"],
                    audio_events=["转场音效"],
                    confidence=0.96,
                ),
                BenchmarkMediaSegment(
                    start_ms=3120,
                    end_ms=6120,
                    description="户外场景揭晓，人物完成同方向动作",
                    shot_type="全景",
                    camera_motion="跟随移动",
                    visual_style="明亮旅行感",
                    transcript="换一个地方，保持动作方向不变。",
                    ocr_text=["动作方向保持一致"],
                    audio_events=["音乐节拍"],
                    confidence=0.91,
                ),
                BenchmarkMediaSegment(
                    start_ms=6120,
                    end_ms=9120,
                    description="左右分屏对比错误与正确动作",
                    shot_type="中景",
                    camera_motion="固定机位",
                    visual_style="教学对比",
                    transcript="关键是两段素材的手要落在同一个位置。",
                    ocr_text=["错误示范", "正确示范"],
                    audio_events=["提示音"],
                    confidence=0.94,
                ),
                BenchmarkMediaSegment(
                    start_ms=9120,
                    end_ms=12_400,
                    description="完整效果连续回放并定格结果",
                    shot_type="全景",
                    camera_motion="匹配剪辑",
                    visual_style="成果展示",
                    transcript="保存下来，下次旅行直接照着拍。",
                    ocr_text=["收藏备用"],
                    audio_events=["音乐收束"],
                    confidence=0.92,
                ),
            ],
        )
    )


def _timeline_item(
    segment: BenchmarkMediaSegment,
    index: int,
) -> BenchmarkDeepTimelineItem:
    evidence_types: list[Literal["frame", "ocr", "asr", "audio"]] = ["frame"]
    if segment.ocr_text:
        evidence_types.append("ocr")
    if segment.transcript:
        evidence_types.append("asr")
    if segment.audio_events:
        evidence_types.append("audio")
    label_parts = [part for part in (segment.shot_type, segment.camera_motion) if part]
    return BenchmarkDeepTimelineItem(
        start_ms=segment.start_ms,
        end_ms=segment.end_ms,
        label=" · ".join(label_parts) or f"镜头 {index + 1}",
        description=segment.description,
        transcript=segment.transcript,
        ocr_text=segment.ocr_text,
        audio_events=segment.audio_events,
        evidence_types=evidence_types,
        confidence=segment.confidence,
        representative_frame_key=segment.representative_frame_key,
    )


def _time_range(start_ms: int, end_ms: int) -> str:
    return f"{_timestamp(start_ms)}–{_timestamp(end_ms)}"


def _timestamp(value: int) -> str:
    seconds = value // 1000
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def _union_duration(intervals: Sequence[tuple[int, int]]) -> int:
    total, boundary = 0, 0
    for start, end in sorted(intervals):
        total += max(0, end - max(boundary, start))
        boundary = max(boundary, end)
    return total


def _pace_interpretation(average_ms: int) -> str:
    if average_ms <= 1800:
        return "快节奏剪辑；需要确认信息是否来得及被看清。"
    if average_ms <= 3500:
        return "中快节奏，适合教程步骤和动作推进。"
    return "长镜头占比较高，更依赖人物表达或氛围保持注意力。"


def _speech_interpretation(rate: int) -> str:
    if rate >= 320:
        return "信息密度偏高，字幕和画面应帮助观众跟上。"
    if rate >= 180:
        return "口播速度处于短视频常用的紧凑区间。"
    return "口播较舒缓，镜头或情绪需要承担更多留存作用。"


def timeline_from_worker_analysis(
    *,
    title: str,
    source_label: str,
    analysis: dict[str, object],
) -> BenchmarkMediaEvidence:
    """Adapt normalized asset-analysis-v3 output without importing the Worker package."""

    technical = analysis.get("technical")
    technical = technical if isinstance(technical, dict) else {}
    temporal = analysis.get("temporal")
    temporal = temporal if isinstance(temporal, dict) else {}
    transcript = analysis.get("transcript")
    transcript = transcript if isinstance(transcript, dict) else {}
    speech_ranges = [
        (int(cue["start_ms"]), int(cue["end_ms"]))
        for cue in transcript.get("segments", [])
        if isinstance(cue, dict) and "start_ms" in cue and "end_ms" in cue
    ]
    raw_segments = analysis.get("segments")
    raw_segments = raw_segments if isinstance(raw_segments, Sequence) else []
    segments: list[BenchmarkMediaSegment] = []
    for raw in raw_segments:
        if not isinstance(raw, dict):
            continue
        segments.append(
            BenchmarkMediaSegment(
                start_ms=int(raw.get("start_ms") or 0),
                end_ms=int(raw.get("end_ms") or 0),
                description=str(raw.get("description") or "未分类镜头"),
                shot_type=_optional_text(raw.get("shot_type")),
                camera_motion=_optional_text(raw.get("camera_motion")),
                visual_style=_optional_text(raw.get("visual_style")),
                transcript=str(raw.get("transcript") or ""),
                ocr_text=_text_list(raw.get("ocr_text")),
                audio_events=_text_list(raw.get("audio_events")),
                confidence=_optional_float(raw.get("confidence")),
                representative_frame_key=_optional_text(raw.get("representative_frame_key")),
            )
        )
    silences = []
    duration_ms = int(technical.get("duration_ms") or 0)
    for item in temporal.get("silences", []):
        if isinstance(item, dict):
            start, end = int(item.get("start_ms") or 0), int(item.get("end_ms") or 0)
            # PCM resampling/codec padding can exceed the video clock by a few ms.
            # Permit only a bounded rounding tail, not arbitrary out-of-range evidence.
            if duration_ms < end <= duration_ms + 50:
                end = duration_ms
            if start < end:
                silences.append((start, end))
    return BenchmarkMediaEvidence(
        source_kind="worker_asset_analysis",
        source_label=source_label,
        title=title,
        duration_ms=int(technical.get("duration_ms") or 0),
        width=_optional_int(technical.get("width")),
        height=_optional_int(technical.get("height")),
        has_audio=technical.get("has_audio") is True,
        speech_status=(
            str(temporal.get("speech_status"))
            if temporal.get("speech_status") in {"present", "absent", "unknown"}
            else "present"
            if any(segment.transcript for segment in segments)
            else "unknown"
        ),
        speech_detection_method=_optional_text(temporal.get("speech_detection_method")),
        analysis_status=(
            analysis.get("status") if analysis.get("status") in {"complete", "partial"} else None
        ),
        sampled_frames=isinstance(analysis.get("sampling"), dict),
        transcript_text=str(transcript["text"])[:100_000] if "text" in transcript else None,
        speech_duration_ms=_union_duration(speech_ranges) or None,
        segments=segments,
        silences=silences,
        limitations=_text_list(analysis.get("limitations")),
    )


def _text_list(value: object) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [str(item).strip()[:200] for item in value if str(item).strip()][:32]


def _optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text[:500] or None


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _optional_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return max(0.0, min(1.0, float(value)))
