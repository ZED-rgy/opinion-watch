from pathlib import Path

from opinion_watch.classification import classify_batch, classify_content
from opinion_watch.models import CollectedContent, OpinionCategory, Platform, RiskSeverity
from opinion_watch.storage import Storage


def test_rule_classifier_filters_content_without_brand_match() -> None:
    result = classify_content(
        {
            "title": "完全无关的普通内容",
            "brand_names": ["速探长"],
            "raw_data": {},
        }
    )

    assert result.category is OpinionCategory.IRRELEVANT
    assert result.severity is RiskSeverity.P3
    assert not result.requires_review


def test_rule_classifier_flags_suspected_false_information_for_review() -> None:
    result = classify_content(
        {
            "title": "速探长被质疑虚假宣传",
            "brand_names": ["速探长"],
            "raw_data": {},
        }
    )

    assert result.category is OpinionCategory.SUSPECTED_FALSE_INFORMATION
    assert result.severity is RiskSeverity.P1
    assert result.requires_review
    assert result.matched_signals == ["虚假宣传"]


def test_rule_classifier_distinguishes_consumer_complaint() -> None:
    result = classify_content(
        {
            "title": "投诉优速卖物流丢件后客服不理",
            "brand_names": ["优速卖"],
            "raw_data": {},
        }
    )

    assert result.category is OpinionCategory.REASONABLE_CONSUMER_COMPLAINT
    assert result.severity is RiskSeverity.P2
    assert result.requires_review


def test_manual_review_is_not_overwritten_by_forced_rule_run(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "test.db")
    storage.initialize()
    storage.add_brand("配达人")
    storage.upsert_contents(
        [
            CollectedContent(
                platform=Platform.DOUYIN,
                content_id="123",
                url="https://www.douyin.com/video/123",
                title="配达人被投诉物流延误",
                source_keyword="配达人",
            )
        ]
    )

    summary = classify_batch(storage)
    assert summary["processed"] == 1
    assessment = storage.list_assessments()[0]
    content_item_id = int(assessment["content_item_id"])
    assert storage.review_assessment(
        content_item_id,
        category=OpinionCategory.ORDINARY_GRIEVANCE.value,
        severity=RiskSeverity.P3.value,
        note="人工确认属于普通吐槽",
        reviewer="tester",
    )

    classify_batch(storage, force=True)
    reviewed = storage.get_assessment(content_item_id)
    assert reviewed is not None
    assert reviewed["source"] == "manual"
    assert reviewed["category"] == OpinionCategory.ORDINARY_GRIEVANCE.value
    assert reviewed["review_status"] == "reviewed"
