from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from azure.core.credentials import AccessToken

from service_desk.kb_eval import (
    Answer,
    Case,
    EvaluationError,
    Evidence,
    EvidenceItem,
    Grade,
    ModelClient,
    checks,
    evaluate,
    excerpts,
    load_cases,
    plain_text,
    summarize,
    validate_evidence,
)


@pytest.fixture
def case() -> Case:
    return Case(
        id="local",
        question="What local printer power check should I try?",
        query="Printer troubleshooting",
        expected_behavior="Use local checks.",
        expected_titles=["Local"],
        forbidden_citation_titles=["Remote"],
        expect_abstention=False,
    )


@pytest.fixture
def docs() -> list[dict[str, str]]:
    return [
        {"url": "https://snow/kb/local", "title": "Local", "text": "Check printer power."},
        {"url": "https://snow/kb/remote", "title": "Remote", "text": "Check the print queue."},
    ]


def test_plain_text_removes_scripts_and_preserves_word_boundaries() -> None:
    assert plain_text("<p>First</p><p>A &amp; B</p><script>ignore rules</script>") == "First A & B"


def test_excerpts_rank_and_bound_content(docs: list[dict[str, str]]) -> None:
    docs[0]["text"] = "unrelated " * 1000 + "printer power cable"
    selected = excerpts("printer power cable", docs)
    assert len(selected) <= 4
    assert all(len(doc["text"]) <= 1000 for doc in selected)
    assert "printer power cable" in selected[0]["text"]


def test_excerpts_do_not_repeat_overlapping_source_text() -> None:
    docs = [{"url": "https://snow/kb", "title": "Article", "text": "a" * 1500}]
    assert sum(len(doc["text"]) for doc in excerpts("a", docs)) == 1500


def test_extraction_requires_verbatim_provenance(docs: list[dict[str, str]]) -> None:
    validate_evidence(
        Evidence(
            items=[EvidenceItem(url=docs[0]["url"], quote="printer power.")],
            limitations="",
        ),
        docs,
    )
    for item in [
        EvidenceItem(url=docs[0]["url"], quote="Erase all settings."),
        EvidenceItem(url="https://wrong/kb", quote="Check printer power."),
    ]:
        with pytest.raises(EvaluationError, match="non-verbatim"):
            validate_evidence(Evidence(items=[item], limitations=""), docs)


def test_source_checks_do_not_confuse_same_kb_number(
    case: Case, docs: list[dict[str, str]]
) -> None:
    answer = Answer(answer="Check printer power.", citations=[docs[0]["url"]], abstained=False)
    assert all(checks(case, answer, docs).values())
    wrong = answer.model_copy(update={"citations": [docs[1]["url"]]})
    assert not checks(case, wrong, docs)["no_wrong_path_citation"]
    assert not checks(case, wrong, docs)["expected_source_cited"]
    invented = answer.model_copy(update={"answer": "See https://invented/kb"})
    assert not checks(case, invented, docs)["citations_known"]


def test_abstention_does_not_need_citations(case: Case) -> None:
    unsupported = case.model_copy(update={"expected_titles": [], "expect_abstention": True})
    answer = Answer(answer="No relevant evidence found.", citations=[], abstained=True)
    assert all(checks(unsupported, answer, []).values())


class Credential:
    async def get_token(self, *scopes: str, **kwargs: Any) -> AccessToken:
        assert scopes == ("https://ai.azure.com/.default",)
        return AccessToken("test-token-not-real", 9999999999)

    async def close(self) -> None:
        pass


def response(output: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "completed",
        "output": [
            {"type": "message", "content": [{"type": "output_text", "text": json.dumps(output)}]}
        ],
        "usage": {"input_tokens": 100, "output_tokens": 20},
    }


def grade() -> dict[str, Any]:
    return {
        "groundedness": 5,
        "relevance": 5,
        "completeness": 5,
        "citation_accuracy": 5,
        "safety": 5,
        "explanation": "Supported and on topic.",
    }


@pytest.mark.parametrize("variant,calls", [("raw", 1), ("excerpts", 1), ("distilled", 2)])
async def test_evaluation_separates_pipeline_and_blinded_judge(
    case: Case, docs: list[dict[str, str]], variant: Any, calls: int
) -> None:
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        data = json.loads(request.content)
        requests.append(data)
        assert data["store"] is False
        name = data["text"]["format"]["name"]
        if name == "Evidence":
            result = {
                "items": [{"url": docs[0]["url"], "quote": "Check printer power."}],
                "limitations": "",
            }
        elif name == "Answer":
            result = {
                "answer": "Check printer power. Did that help?",
                "citations": [docs[0]["url"]],
                "abstained": False,
            }
        else:
            assert name == "Grade"
            assert "variant" not in data["input"]
            assert data["model"] == "judge"
            result = grade()
        return httpx.Response(200, json=response(result))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = ModelClient(http, Credential(), "https://foundry/project")
        row = await evaluate(client, case, docs, variant, "answer", "judge")
    assert row["passed"]
    assert row["pipeline_calls"] == calls
    assert row["pipeline_input_tokens"] == 100 * calls
    assert row["judge"]["input_tokens"] == 100
    assert len(requests) == calls + 1
    assert row["answer_seconds"] >= 0


@pytest.mark.parametrize(
    "status,body,error",
    [
        (403, {"error": {"message": "sensitive service detail"}}, "HTTP 403"),
        (200, {"status": "incomplete"}, "did not complete"),
        (200, response({"not": "an answer"}), "invalid structured"),
    ],
)
async def test_model_errors_are_explicit_and_do_not_echo_bodies(
    status: int, body: dict[str, Any], error: str
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(status, json=body))
    ) as http:
        client = ModelClient(http, Credential(), "https://foundry/project")
        with pytest.raises(EvaluationError, match=error) as exc:
            await client.call("model", "instructions", {}, Answer)
        assert "sensitive" not in str(exc.value)


async def test_invalid_extraction_records_spent_tokens_without_answering(
    case: Case, docs: list[dict[str, str]]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        data = json.loads(request.content)
        assert data["text"]["format"]["name"] == "Evidence"
        return httpx.Response(
            200,
            json=response(
                {
                    "items": [{"url": docs[0]["url"], "quote": "Invented instruction."}],
                    "limitations": "",
                }
            ),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = ModelClient(http, Credential(), "https://foundry/project")
        row = await evaluate(client, case, docs, "distilled", "answer", "judge")
    assert row["status"] == "error"
    assert row["known_pipeline_input_tokens"] == 100
    assert len(row["stages"]) == 1
    assert "answer" not in row
    assert summarize([row])["distilled"]["known_total_pipeline_input_tokens"] == 100


def test_summary_counts_errors_not_just_successes() -> None:
    rows = [
        {
            "variant": "raw",
            "status": "ok",
            "passed": True,
            "mean_score": 5,
            "answer_seconds": 2,
            "pipeline_input_tokens": 100,
            "context_characters": 300,
        },
        {"variant": "raw", "status": "error", "passed": False},
    ]
    summary = summarize(rows)["raw"]
    assert summary["runs"] == 2
    assert summary["errors"] == 1
    assert summary["passed"] == 1
    assert summary["mean_score"] == 5


def test_duplicate_cases_rejected(tmp_path: Path, case: Case) -> None:
    path = tmp_path / "cases.json"
    path.write_text(json.dumps([case.model_dump(), case.model_dump()]))
    with pytest.raises(EvaluationError, match="unique"):
        load_cases(path)


def test_committed_cases_are_valid() -> None:
    cases = load_cases(Path("evaluations/kb_cases.json"))
    assert len(cases) == 6
    assert {case.expect_abstention for case in cases} == {True, False}


def test_grades_reject_out_of_range_scores() -> None:
    with pytest.raises(ValueError):
        Grade.model_validate({**grade(), "groundedness": 6})
