# ruff: noqa: RUF001 - Chinese punctuation is intentional in user-facing report copy.

from __future__ import annotations

import re
import statistics
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from .benchmark_accounts import BenchmarkNote, BenchmarkSnapshot


class BenchmarkReportModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BenchmarkStrategySignal(BenchmarkReportModel):
    key: str
    label: str
    evidence: str
    likely_effect: str
    reusable_move: str


class BenchmarkInsight(BenchmarkReportModel):
    level: Literal["observed", "derived", "inference", "limitation"]
    confidence: Literal["high", "medium", "low"]
    claim: str
    evidence: list[str]


class BenchmarkNotePerformance(BenchmarkReportModel):
    tier: Literal[
        "top_candidate", "above_baseline", "baseline", "below_baseline", "unknown"
    ]
    percentile: float | None = Field(default=None, ge=0, le=100)
    relative_to_median: float | None = Field(default=None, ge=0)
    sample_median_likes_lower_bound: int | None = Field(default=None, ge=0)
    caveat: str


class BenchmarkNoteReport(BenchmarkReportModel):
    sample_index: int = Field(ge=1)
    title: str
    format: Literal["video", "image", "unknown"]
    published_at: datetime | None
    likes_display: str
    likes_lower_bound: int | None = Field(default=None, ge=0)
    performance: BenchmarkNotePerformance
    strategy_signals: list[BenchmarkStrategySignal]
    viral_mechanisms: list[BenchmarkInsight]
    recommendations: list[str]
    evidence_depth: Literal["public_metadata_only"] = "public_metadata_only"
    limitations: list[str]


class BenchmarkTitlePattern(BenchmarkReportModel):
    key: str
    label: str
    matching_notes: int = Field(ge=1)
    share_percent: float = Field(ge=0, le=100)
    top_candidate_matches: int = Field(ge=0)
    median_likes_lower_bound: int | None = Field(default=None, ge=0)
    lift_vs_sample_median: float | None = Field(default=None, ge=0)


class BenchmarkAccountStrategyReport(BenchmarkReportModel):
    nickname: str
    sample_size: int = Field(ge=0)
    evidence_depth: Literal["public_metadata_only"] = "public_metadata_only"
    executive_summary: str
    format_strategy: str
    publishing_strategy: str
    title_patterns: list[BenchmarkTitlePattern]
    top_candidate_note_indexes: list[int]
    playbook: list[str]
    limitations: list[str]


class BenchmarkAccountReport(BenchmarkReportModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    generated_at: datetime
    snapshot: BenchmarkSnapshot
    account_report: BenchmarkAccountStrategyReport
    note_reports: list[BenchmarkNoteReport]
    history_record_id: UUID | None = None
    history_warning: str | None = None


@dataclass(frozen=True, slots=True)
class _PatternRule:
    key: str
    label: str
    matcher: Callable[[str], str | None]
    likely_effect: str
    reusable_move: str


def _keyword_matcher(*keywords: str) -> Callable[[str], str | None]:
    def match(title: str) -> str | None:
        matches = [keyword for keyword in keywords if keyword.casefold() in title.casefold()]
        if not matches:
            return None
        return f"标题出现“{'、'.join(matches[:3])}”"

    return match


def _regex_matcher(pattern: str, evidence: str) -> Callable[[str], str | None]:
    compiled = re.compile(pattern, re.IGNORECASE)

    def match(title: str) -> str | None:
        return evidence if compiled.search(title) else None

    return match


def _minimal_title(title: str) -> str | None:
    visible = re.sub(r"[^\w\u4e00-\u9fff]+", "", title, flags=re.UNICODE)
    if len(visible) <= 7:
        return f"去除符号后仅 {len(visible)} 个字符，采用极简留白"
    return None


_PATTERNS = (
    _PatternRule(
        "audience-dialogue",
        "对话式互动",
        _regex_matcher(r"[吗？?]|要不要|欢迎|请|你们|和我|来，", "标题直接向读者提问或发出邀请"),
        "把浏览者放进对话关系，可能提升停留和评论意愿。",
        "用一个能自然回答的问题开场，并让互动动作与内容主题相关。",
    ),
    _PatternRule(
        "first-person-story",
        "第一人称代入",
        _regex_matcher(r"我|我们|女孩|人生|十八岁", "标题使用第一人称、身份或人生阶段"),
        "强化人格与经历感，让内容更像一个可跟随的故事。",
        "先给出具体身份或处境，再展示变化，不复制原作者人设。",
    ),
    _PatternRule(
        "contrast-turn",
        "反差与转折",
        _regex_matcher(
            r"从.+到|但|只是|又|意想不到|最后|更短|一种很新",
            "标题包含变化、反差或意外信号",
        ),
        "制造认知缺口，促使读者继续寻找变化发生的过程。",
        "用真实的前后差异承诺结果，并在内容中兑现，不做空悬念。",
    ),
    _PatternRule(
        "method-promise",
        "方法与拍摄承诺",
        _keyword_matcher("转场", "vlog", "拍", "视角", "封面", "记录方式"),
        "提供明确的内容用途，可能提高收藏与转发动机。",
        "标题同时说清使用场景和能带走的方法。",
    ),
    _PatternRule(
        "sensory-season",
        "季节与感官意象",
        _keyword_matcher("夏天", "夏日", "阳光", "绿色", "薄荷", "茉莉", "青提", "美食"),
        "颜色、味觉和季节词能迅速建立画面与情绪预期。",
        "选择一个可被视觉兑现的感官词，贯穿封面、标题和正文。",
    ),
    _PatternRule(
        "place-scene",
        "地点与场景锚点",
        _keyword_matcher("旅行", "清迈", "香港", "Hong Kong", "巴黎", "世界", "厨房"),
        "具体地点降低理解成本，也增加搜索和向往场景。",
        "使用真实、具体的地点，并补充一个独特体验或动作。",
    ),
    _PatternRule(
        "number-specificity",
        "数字具体化",
        _regex_matcher(r"\d|[一二三四五六七八九十百]", "标题使用数量或年龄等数字信息"),
        "明确数量让内容边界和信息密度更容易被预估。",
        "数字必须对应内容中可核验的条目、阶段或结果。",
    ),
    _PatternRule(
        "minimal-curiosity",
        "极简悬念",
        _minimal_title,
        "较少信息依赖封面或首帧补全语义，可能形成好奇，但也更依赖账号认知。",
        "只在画面能够独立表达主题时使用极简标题。",
    ),
)


def build_benchmark_account_report(snapshot: BenchmarkSnapshot) -> BenchmarkAccountReport:
    note_reports = _build_note_reports(snapshot)
    account_report = _build_account_report(snapshot, note_reports)
    return BenchmarkAccountReport(
        generated_at=snapshot.acquisition.captured_at,
        snapshot=snapshot,
        account_report=account_report,
        note_reports=note_reports,
    )


def _build_note_reports(snapshot: BenchmarkSnapshot) -> list[BenchmarkNoteReport]:
    known_likes = [
        note.likes.lower_bound
        for note in snapshot.notes
        if note.likes.lower_bound is not None
    ]
    sample_median = int(statistics.median(known_likes)) if known_likes else None
    return [
        _build_note_report(note, snapshot=snapshot, values=known_likes, median=sample_median)
        for note in snapshot.notes
    ]


def _build_note_report(
    note: BenchmarkNote,
    *,
    snapshot: BenchmarkSnapshot,
    values: list[int],
    median: int | None,
) -> BenchmarkNoteReport:
    performance = _performance(note, values=values, median=median)
    signals = _strategy_signals(note.title)
    mechanisms = [
        BenchmarkInsight(
            level="inference",
            confidence="medium",
            claim=f"{signal.label}可能是这条内容获得注意力的一个机制。",
            evidence=[signal.evidence, f"公开点赞展示为 {note.likes.display}"],
        )
        for signal in signals[:3]
    ]
    mechanisms.append(
        BenchmarkInsight(
            level="derived",
            confidence="high",
            claim=f"该笔记在当前首屏样本中处于“{_tier_label(performance.tier)}”。",
            evidence=[
                _performance_evidence(performance),
                "比较仅使用同账号当前公开首屏样本。",
            ],
        )
    )
    if note.pinned:
        mechanisms.append(
            BenchmarkInsight(
                level="limitation",
                confidence="high",
                claim="置顶位置可能增加曝光，不能把互动表现全部归因于内容策略。",
                evidence=["主页公开数据将该笔记标记为置顶。"],
            )
        )
    mechanisms.append(
        BenchmarkInsight(
            level="limitation",
            confidence="high",
            claim="缺少曝光、完播、分享和发布时间序列，无法证明平台推荐的因果原因。",
            evidence=["当前采集深度为公开主页元数据。"],
        )
    )
    recommendations = [signal.reusable_move for signal in signals[:3]]
    if not recommendations:
        recommendations.append("先补充正文、封面或视频首帧证据，再提炼可复用策略。")
    return BenchmarkNoteReport(
        sample_index=note.sample_index,
        title=note.title,
        format=note.format,
        published_at=note.published_at,
        likes_display=note.likes.display,
        likes_lower_bound=note.likes.lower_bound,
        performance=performance,
        strategy_signals=signals,
        viral_mechanisms=mechanisms,
        recommendations=recommendations,
        limitations=[
            "尚未取得正文、评论、收藏、分享、曝光或完播数据。",
            "视频未做 ASR、OCR 和镜头分析；图文未读取多图顺序和图片文字。",
            "“爆款候选”是账号内相对表现标签，不等同于平台官方爆款结论。",
        ],
    )


def _strategy_signals(title: str) -> list[BenchmarkStrategySignal]:
    output: list[BenchmarkStrategySignal] = []
    for rule in _PATTERNS:
        evidence = rule.matcher(title)
        if evidence is None:
            continue
        output.append(
            BenchmarkStrategySignal(
                key=rule.key,
                label=rule.label,
                evidence=evidence,
                likely_effect=rule.likely_effect,
                reusable_move=rule.reusable_move,
            )
        )
    return output


def _performance(
    note: BenchmarkNote,
    *,
    values: list[int],
    median: int | None,
) -> BenchmarkNotePerformance:
    current = note.likes.lower_bound
    if current is None or not values or median is None:
        return BenchmarkNotePerformance(
            tier="unknown",
            percentile=None,
            relative_to_median=None,
            sample_median_likes_lower_bound=median,
            caveat="公开互动量无法转换为可比较下界。",
        )
    less = sum(value < current for value in values)
    equal = sum(value == current for value in values)
    percentile = round((less + equal / 2) / len(values) * 100, 1)
    if percentile >= 75:
        tier = "top_candidate"
    elif percentile >= 60:
        tier = "above_baseline"
    elif percentile >= 30:
        tier = "baseline"
    else:
        tier = "below_baseline"
    return BenchmarkNotePerformance(
        tier=tier,
        percentile=percentile,
        relative_to_median=round(current / median, 2) if median else None,
        sample_median_likes_lower_bound=median,
        caveat="带“+”或“万”的平台展示值按下界或约数比较，未按曝光量归一化。",
    )


def _build_account_report(
    snapshot: BenchmarkSnapshot,
    reports: list[BenchmarkNoteReport],
) -> BenchmarkAccountStrategyReport:
    pattern_reports: dict[str, list[BenchmarkNoteReport]] = defaultdict(list)
    labels: dict[str, str] = {}
    for report in reports:
        for signal in report.strategy_signals:
            pattern_reports[signal.key].append(report)
            labels[signal.key] = signal.label
    sample_median = snapshot.analysis.median_likes_lower_bound
    title_patterns = [
        _aggregate_pattern(
            key,
            labels[key],
            matches,
            sample_size=len(reports),
            sample_median=sample_median,
        )
        for key, matches in pattern_reports.items()
    ]
    title_patterns.sort(
        key=lambda item: (
            -item.top_candidate_matches,
            -(item.lift_vs_sample_median or 0),
            -item.matching_notes,
            item.label,
        )
    )
    top_indexes = [
        report.sample_index
        for report in reports
        if report.performance.tier == "top_candidate"
    ]
    repeated = [pattern for pattern in title_patterns if pattern.matching_notes >= 2]
    playbook = [
        (
            f"{pattern.label}：首屏出现 {pattern.matching_notes} 次，其中 "
            f"{pattern.top_candidate_matches} 次进入高表现候选；先作为可复测假设。"
        )
        for pattern in repeated[:4]
    ]
    playbook.append("复刻结构，不复制人物设定、原句、封面或具体镜头。")
    format_strategy = (
        f"首屏 {snapshot.analysis.sample_size} 篇中，视频 {snapshot.analysis.video_count} 篇、"
        f"图文 {snapshot.analysis.image_count} 篇，"
        f"另有 {snapshot.analysis.unknown_count} 篇媒体类型待详情确认，"
        f"视频占比 {snapshot.analysis.video_share_percent}%。"
    )
    cadence = snapshot.analysis.median_publish_interval_days
    publishing_strategy = (
        f"非置顶样本的发布间隔中位数约 {cadence} 天，近 30 天可见 "
        f"{snapshot.analysis.posts_last_30_days} 篇。"
        if cadence is not None
        else f"近 30 天可见 {snapshot.analysis.posts_last_30_days} 篇，样本不足以计算稳定间隔。"
    )
    if not any(note.published_at for note in snapshot.notes):
        publishing_strategy = "样本未提供可核验发布时间，无法统计近 30 天发布量或发布间隔。"
    return BenchmarkAccountStrategyReport(
        nickname=snapshot.profile.nickname,
        sample_size=len(reports),
        executive_summary=(
            f"当前公开首屏共 {len(reports)} 篇，识别出 {len(top_indexes)} 篇账号内高表现候选。"
            "报告用标题策略和账号内互动分布解释可见差异，不把相关性写成平台推荐因果。"
        ),
        format_strategy=format_strategy,
        publishing_strategy=publishing_strategy,
        title_patterns=title_patterns,
        top_candidate_note_indexes=top_indexes,
        playbook=playbook,
        limitations=[
            "主页仍有更多内容，当前不是全量账号报告。",
            "匿名 SSR 会降低粉丝和互动总量精度。",
            "正文、封面视觉、视频语音和镜头尚未进入证据链。",
            "没有曝光、完播和分享数据，爆款原因只能表达为可复测机制假设。",
        ],
    )


def _aggregate_pattern(
    key: str,
    label: str,
    reports: list[BenchmarkNoteReport],
    *,
    sample_size: int,
    sample_median: int | None,
) -> BenchmarkTitlePattern:
    likes = [
        value
        for report in reports
        if (value := _report_likes_lower_bound(report)) is not None
    ]
    pattern_median = int(statistics.median(likes)) if likes else None
    return BenchmarkTitlePattern(
        key=key,
        label=label,
        matching_notes=len(reports),
        share_percent=round(len(reports) / sample_size * 100, 1) if sample_size else 0,
        top_candidate_matches=sum(
            report.performance.tier == "top_candidate" for report in reports
        ),
        median_likes_lower_bound=pattern_median,
        lift_vs_sample_median=(
            round(pattern_median / sample_median, 2)
            if pattern_median is not None and sample_median
            else None
        ),
    )


def _report_likes_lower_bound(report: BenchmarkNoteReport) -> int | None:
    return report.likes_lower_bound


def _tier_label(tier: str) -> str:
    return {
        "top_candidate": "高表现候选",
        "above_baseline": "高于基线",
        "baseline": "账号基线",
        "below_baseline": "低于基线",
        "unknown": "无法比较",
    }.get(tier, "无法比较")


def _performance_evidence(performance: BenchmarkNotePerformance) -> str:
    if performance.percentile is None:
        return "公开互动量不足以计算样本内位置。"
    ratio = performance.relative_to_median
    ratio_text = f"，约为样本中位数的 {ratio} 倍" if ratio is not None else ""
    return f"账号内互动下界百分位约 P{performance.percentile}{ratio_text}。"
