import asyncio
import json
from pathlib import Path
from urllib.error import HTTPError

from opinion_watch.classification import classify_batch
from opinion_watch.credentials import CredentialStore
from opinion_watch.llm import (
    LLMAssessment,
    LLMCallBudget,
    LLMClient,
    LLMSettings,
    build_assessment_message,
    build_assessment_prompt,
    classify_with_llm,
    parse_assessment,
)
from opinion_watch.models import CollectedContent, OpinionCategory, Platform, RiskSeverity
from opinion_watch.storage import Storage


def test_parse_assessment_normalizes_json_response() -> None:
    assessment = parse_assessment(
        '```json\n{"category":"suspected_defamation","severity":"P0",'
        '"confidence":1.5,"summary":"需要人工确认","evidence":["出现指控"],'
        '"needs_review":false}\n```'
    )

    assert assessment.category is OpinionCategory.SUSPECTED_DEFAMATION
    assert assessment.severity is RiskSeverity.P1
    assert assessment.confidence == 1.0
    assert assessment.requires_review
    assert assessment.matched_signals == ["出现指控"]


def test_build_assessment_prompt_contains_public_content_fields() -> None:
    prompt = build_assessment_prompt(
        {
            "title": "品牌售后争议",
            "author_name": "用户甲",
            "brand_names": ["示例品牌"],
            "raw_data": {"description": "公开描述", "comments": ["评论一"]},
        }
    )

    assert "示例品牌" in prompt
    assert "公开描述" in prompt
    assert "评论一" in prompt


def test_build_assessment_message_is_text_only_even_when_media_exists() -> None:
    message = build_assessment_message(
        {
            "title": "图片证据",
            "brand_names": ["示例品牌"],
            "raw_data": {
                "media": [
                    {"kind": "image", "evidence_path": "src/opinion_watch/assets/empty-scan.png"}
                ]
            },
        }
    )

    assert isinstance(message, str)
    assert "图片证据" in message


def test_llm_assess_sends_text_only_message() -> None:
    client = LLMClient(LLMSettings("openai-compatible", "https://example.test/v1", "demo", "key"))
    messages: list[list[dict[str, object]]] = []

    async def fake_complete(request_messages):
        messages.append(request_messages)
        return json.dumps(
            {
                "category": "ordinary_grievance",
                "severity": "P2",
                "confidence": 0.8,
                "summary": "文本复核完成",
                "evidence": ["文本证据"],
                "needs_review": True,
            }
        )

    client._complete = fake_complete  # type: ignore[method-assign]
    assessment = asyncio.run(
        client.assess(
            {
                "title": "含图片的公开内容",
                "brand_names": ["示例品牌"],
                "raw_data": {"media": [{"url": "https://example.test/image.png"}]},
            }
        )
    )

    assert assessment.category is OpinionCategory.ORDINARY_GRIEVANCE
    assert len(messages) == 1
    assert isinstance(messages[0][1]["content"], str)


def test_llm_client_uses_openai_compatible_chat_endpoint(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self) -> bytes:
            return json.dumps({"choices": [{"message": {"content": '{"ok":true}'}}]}).encode()

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["authorization"] = request.get_header("Authorization")
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr("opinion_watch.llm.urlopen", fake_urlopen)
    client = LLMClient(LLMSettings("openai-compatible", "https://example.test/v1", "demo", "key"))
    assert client._request({"model": "demo"}) == '{"ok":true}'
    assert captured == {
        "url": "https://example.test/v1/chat/completions",
        "authorization": "Bearer key",
        "timeout": 60,
    }


def test_llm_error_does_not_expose_api_key(monkeypatch) -> None:
    class FakeResponse:
        def read(self) -> bytes:
            return b'{"error":{"message":"bad"}}'

        def close(self) -> None:
            pass

    def fake_urlopen(_request, timeout):
        raise HTTPError(
            "https://example.test/v1/chat/completions",
            401,
            "Unauthorized",
            {},
            FakeResponse(),
        )

    monkeypatch.setattr("opinion_watch.llm.urlopen", fake_urlopen)
    client = LLMClient(
        LLMSettings("openai-compatible", "https://example.test/v1", "demo", "sk-secret")
    )

    try:
        client._request({"model": "demo"})
    except Exception as exc:
        message = str(exc)
    else:
        raise AssertionError("expected an authentication error")

    assert "sk-secret" not in message
    assert "鉴权失败" in message


def test_classify_with_llm_updates_model_assessment(tmp_path: Path, monkeypatch) -> None:
    storage = Storage(tmp_path / "test.db")
    storage.initialize()
    storage.add_brand("示例品牌")
    storage.upsert_contents(
        [
            CollectedContent(
                platform=Platform.DOUYIN,
                content_id="llm-1",
                url="https://example.test/llm-1",
                title="示例品牌被投诉售后不理",
                source_keyword="示例品牌",
            )
        ]
    )
    classify_batch(storage)
    storage.save_llm_config(
        enabled=True,
        provider="openai-compatible",
        base_url="https://example.test/v1",
        model="demo",
    )

    monkeypatch.setattr(
        CredentialStore,
        "get_llm_api_key",
        classmethod(lambda _cls: "test-key"),
    )

    async def fake_assess(_self, _content):
        return LLMAssessment(
            category=OpinionCategory.ORDINARY_GRIEVANCE,
            severity=RiskSeverity.P2,
            confidence=0.9,
            rationale="模型复判摘要：属于普通消费纠纷",
            matched_signals=["售后不理"],
            requires_review=True,
        )

    monkeypatch.setattr(LLMClient, "assess", fake_assess)
    result = asyncio.run(classify_with_llm(storage))

    assert result["processed"] == 1
    assessment = storage.list_assessments()[0]
    assert assessment["source"] == "model"
    assert assessment["category"] == OpinionCategory.ORDINARY_GRIEVANCE.value


def test_llm_call_budget_is_shared_and_never_exceeded() -> None:
    budget = LLMCallBudget(limit=2)
    assert budget.take(5) == 2
    assert budget.take(1) == 0
    assert budget.used == 2


def test_llm_capability_probe_records_text_and_multimodal_support() -> None:
    client = LLMClient(LLMSettings("openai-compatible", "https://example.test/v1", "demo", "key"))
    requests: list[list[dict[str, object]]] = []

    async def fake_complete(messages):
        requests.append(messages)
        return '{"ok":true}'

    client._complete = fake_complete  # type: ignore[method-assign]
    capabilities = asyncio.run(client.probe_capabilities())

    assert capabilities.chat_completions is True
    assert capabilities.text_messages is True
    assert capabilities.multimodal_messages is False
    assert len(requests) == 1
