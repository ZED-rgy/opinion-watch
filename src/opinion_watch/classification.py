from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from opinion_watch.models import OpinionCategory, RiskSeverity
from opinion_watch.storage import Storage


@dataclass(frozen=True, slots=True)
class AssessmentResult:
    category: OpinionCategory
    severity: RiskSeverity
    confidence: float
    rationale: str
    matched_signals: list[str]
    requires_review: bool


def is_suspected(result: AssessmentResult) -> bool:
    """Return whether a shallow result deserves a detail-page investigation."""
    return result.category not in {OpinionCategory.OTHER, OpinionCategory.IRRELEVANT} and (
        result.requires_review or result.severity in {RiskSeverity.P1, RiskSeverity.P2}
    )


_FALSE_INFORMATION_SIGNALS = (
    "虚假宣传",
    "虚假信息",
    "造假",
    "假货",
    "伪造",
    "不实信息",
)
_DEFAMATION_SIGNALS = (
    "骗子",
    "诈骗",
    "黑心公司",
    "垃圾公司",
    "恶心公司",
    "跑路",
    "骗钱",
)
_COORDINATED_SIGNALS = (
    "集体投诉",
    "集中投诉",
    "多人投诉",
    "大规模投诉",
    "联合维权",
)
_ASTROTURFING_SIGNALS = (
    "水军攻击",
    "恶意刷屏",
    "恶意攻击",
    "带节奏",
)
_COMPLAINT_SIGNALS = (
    "投诉",
    "举报",
    "维权",
    "曝光",
    "避雷",
    "差评",
    "不退款",
    "不赔付",
    "拒赔",
    "丢件",
    "延误",
    "拖欠",
    "乱收费",
    "客服不理",
    "联系不上",
)
_SERVICE_SIGNALS = (
    "退款",
    "赔付",
    "客服",
    "物流",
    "快递",
    "包裹",
    "发货",
    "收费",
    "时效",
    "售后",
    "合同",
)
_ORDINARY_NEGATIVE_SIGNALS = (
    "不好",
    "很差",
    "太差",
    "失望",
    "太慢",
    "很慢",
    "太贵",
    "踩坑",
    "坑人",
    "不推荐",
    "体验差",
)


def classify_content(content: dict[str, Any]) -> AssessmentResult:
    text = _content_text(content)
    brands = [str(item) for item in content.get("brand_names", [])]
    relevant_signals = (
        _FALSE_INFORMATION_SIGNALS
        + _DEFAMATION_SIGNALS
        + _COORDINATED_SIGNALS
        + _ASTROTURFING_SIGNALS
        + _COMPLAINT_SIGNALS
        + _SERVICE_SIGNALS
        + _ORDINARY_NEGATIVE_SIGNALS
    )
    if (
        brands
        and not any(brand.lower() in text for brand in brands)
        and not _matched_signals(text, relevant_signals)
    ):
        return AssessmentResult(
            category=OpinionCategory.IRRELEVANT,
            severity=RiskSeverity.P3,
            confidence=0.85,
            rationale="可提取文本未出现品牌精确名称，暂按搜索噪声过滤，不生成待复核播报。",
            matched_signals=[],
            requires_review=False,
        )
    # 命中负面信号但全文没出现品牌名：多为搜索排序带出的它牌负面或
    # 泛化吐槽。不能按品牌舆情的等级定级，否则台账里全是误报。
    # 降为 P3 并保留复核标记，由人工或大模型二次判断归属。
    brand_matched = not brands or any(brand.lower() in text for brand in brands)

    rules = (
        (
            _COORDINATED_SIGNALS,
            OpinionCategory.COORDINATED_COMPLAINT,
            RiskSeverity.P1,
            0.8,
            "命中集中投诉或联合维权信号，需要核查传播规模与投诉真实性。",
        ),
        (
            _ASTROTURFING_SIGNALS,
            OpinionCategory.SUSPECTED_ASTROTURFING,
            RiskSeverity.P1,
            0.65,
            "命中疑似水军或恶意攻击信号，仅作为人工调查线索。",
        ),
        (
            _FALSE_INFORMATION_SIGNALS,
            OpinionCategory.SUSPECTED_FALSE_INFORMATION,
            RiskSeverity.P1,
            0.7,
            "命中疑似虚假信息信号，需要结合事实和权利材料人工核验。",
        ),
        (
            _DEFAMATION_SIGNALS,
            OpinionCategory.SUSPECTED_DEFAMATION,
            RiskSeverity.P1,
            0.65,
            "命中强烈负面指控信号，不代表法律定性，必须人工复核。",
        ),
    )
    for signals, category, severity, confidence, rationale in rules:
        matched = _matched_signals(text, signals)
        if matched:
            if not brand_matched:
                return AssessmentResult(
                    category=category,
                    severity=RiskSeverity.P3,
                    confidence=0.5,
                    rationale=(
                        "命中负面信号但可提取文本未出现品牌名称，可能是同页其他主体的内容；"
                        "降级为低优先级线索，待人工确认归属。"
                    ),
                    matched_signals=matched,
                    requires_review=True,
                )
            return AssessmentResult(
                category=category,
                severity=severity,
                confidence=confidence,
                rationale=rationale,
                matched_signals=matched,
                requires_review=True,
            )

    complaint_matches = _matched_signals(text, _COMPLAINT_SIGNALS)
    service_matches = _matched_signals(text, _SERVICE_SIGNALS)
    if complaint_matches and service_matches:
        if not brand_matched:
            return AssessmentResult(
                category=OpinionCategory.REASONABLE_CONSUMER_COMPLAINT,
                severity=RiskSeverity.P3,
                confidence=0.5,
                rationale=(
                    "命中投诉信号但可提取文本未出现品牌名称；降级为低优先级线索，待人工确认归属。"
                ),
                matched_signals=[*complaint_matches, *service_matches],
                requires_review=True,
            )
        return AssessmentResult(
            category=OpinionCategory.REASONABLE_CONSUMER_COMPLAINT,
            severity=RiskSeverity.P2,
            confidence=0.75,
            rationale="同时命中消费服务场景和投诉信号，优先按合理消费者投诉复核。",
            matched_signals=[*complaint_matches, *service_matches],
            requires_review=True,
        )

    ordinary_matches = _matched_signals(text, _ORDINARY_NEGATIVE_SIGNALS)
    if complaint_matches or ordinary_matches:
        return AssessmentResult(
            category=OpinionCategory.ORDINARY_GRIEVANCE,
            severity=RiskSeverity.P3,
            confidence=0.7,
            rationale="命中一般负面或吐槽信号，当前未发现高风险指控。",
            matched_signals=[*complaint_matches, *ordinary_matches],
            requires_review=False,
        )

    return AssessmentResult(
        category=OpinionCategory.OTHER,
        severity=RiskSeverity.P3,
        confidence=0.6,
        rationale="未命中当前负面规则，暂归为其他内容。",
        matched_signals=[],
        requires_review=False,
    )


def classify_batch(
    storage: Storage,
    *,
    limit: int = 100,
    force: bool = False,
    run_id: int | None = None,
) -> dict[str, Any]:
    contents = storage.list_contents_for_assessment(
        limit=limit,
        include_assessed=force,
        run_id=run_id,
    )
    counts: dict[str, int] = {}
    review_count = 0
    for content in contents:
        result = classify_content(content)
        storage.upsert_assessment(
            content_item_id=int(content["id"]),
            category=result.category.value,
            severity=result.severity.value,
            confidence=result.confidence,
            rationale=result.rationale,
            matched_signals=result.matched_signals,
            requires_review=result.requires_review,
        )
        counts[result.category.value] = counts.get(result.category.value, 0) + 1
        review_count += int(result.requires_review)
    return {
        "processed": len(contents),
        "requires_review": review_count,
        "categories": counts,
    }


def _content_text(content: dict[str, Any]) -> str:
    raw_data = content.get("raw_data")
    raw: dict[str, Any] = raw_data if isinstance(raw_data, dict) else {}
    parts = [
        str(content.get("title") or ""),
        str(raw.get("page_title") or ""),
        str(raw.get("description") or ""),
        str(raw.get("search_card_text") or ""),
    ]
    comments = raw.get("comments")
    if isinstance(comments, list):
        parts.extend(str(comment) for comment in comments[:100])
    return "\n".join(parts).lower()


def _matched_signals(text: str, signals: tuple[str, ...]) -> list[str]:
    return [signal for signal in signals if signal.lower() in text]
