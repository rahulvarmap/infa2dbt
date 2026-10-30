from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from artifacts.infa2dbt.api.main import app
from api.processing_worker import process_xml_request


SAMPLES_DIR = Path(__file__).resolve().parents[2] / "samples"
SAMPLE_NAMES = [
    "m_member_enrollment.xml",
    "m_claims_member_join.xml",
    "m_claims_provider_lookup.xml",
    "m_claims_member_summary.xml",
]


@pytest.mark.parametrize("sample_name", SAMPLE_NAMES)
def test_each_bundled_sample_has_a_complete_deterministic_explanation(
    sample_name: str,
) -> None:
    response = process_xml_request(
        "explain",
        (SAMPLES_DIR / sample_name).read_bytes(),
        {"dialect": "snowflake"},
    )
    assert response["status_code"] == 200
    explanations = response["content"]["explanations"]
    assert len(explanations) == 1
    explanation = next(iter(explanations.values()))

    assert explanation["overview"]
    assert explanation["steps"]
    assert explanation["columns"]
    assert explanation["generated_models"]
    assert all(step["description"] for step in explanation["steps"])
    assert all(column["rule"] for column in explanation["columns"])
    assert any(
        model["path"].startswith("models/staging/")
        for model in explanation["generated_models"]
    )
    assert any(
        model["path"].startswith("models/marts/")
        for model in explanation["generated_models"]
    )
    assert explanation["migration_status"]["supported"] == 1
    assert explanation["migration_status"]["blocked"] == 0


def test_explainer_uses_ast_for_filter_join_lookup_and_aggregator_rules() -> None:
    enrollment = process_xml_request(
        "explain",
        (SAMPLES_DIR / "m_member_enrollment.xml").read_bytes(),
        {},
    )["content"]["explanations"]["m_member_enrollment"]
    assert "keeping active members" in enrollment["overview"]
    full_name = next(
        item for item in enrollment["columns"]
        if item["target_column"] == "FULL_NAME"
    )
    assert full_name["source_columns"] == [
        "MEMBER_ENROLLMENT.FIRST_NAME",
        "MEMBER_ENROLLMENT.LAST_NAME",
    ]
    assert "Joined from" in full_name["rule"]
    assert "date of birth" in next(
        item["rule"] for item in enrollment["columns"]
        if item["target_column"] == "AGE"
    ).lower()
    assert any(
        "keeps rows where" in step["description"].lower()
        for step in enrollment["steps"]
    )

    join = process_xml_request(
        "explain",
        (SAMPLES_DIR / "m_claims_member_join.xml").read_bytes(),
        {},
    )["content"]["explanations"]["m_claims_member_join"]
    join_step = next(step for step in join["steps"] if step["type"] == "Joiner")
    assert "Normal join" in join_step["description"]
    assert "master" in join_step["description"]
    assert "detail" in join_step["description"]

    lookup = process_xml_request(
        "explain",
        (SAMPLES_DIR / "m_claims_provider_lookup.xml").read_bytes(),
        {},
    )["content"]["explanations"]["m_claims_provider_lookup"]
    lookup_step = next(step for step in lookup["steps"] if step["type"] == "Lookup")
    assert "on no match" in lookup_step["description"]
    assert "'UNKNOWN PROVIDER'" in lookup_step["description"]
    assert "'UNASSIGNED'" in lookup_step["description"]

    summary = process_xml_request(
        "explain",
        (SAMPLES_DIR / "m_claims_member_summary.xml").read_bytes(),
        {},
    )["content"]["explanations"]["m_claims_member_summary"]
    aggregate_step = next(
        step for step in summary["steps"] if step["type"] == "Aggregator"
    )
    assert "Groups by Member ID" in aggregate_step["description"]
    assert "Sum of Claim amount for rows where" in aggregate_step["description"]
    paid = next(item for item in summary["columns"] if item["target_column"] == "PAID_AMOUNT")
    assert paid["rule"] == "Sum of Claim amount for rows where Claim status equals PAID"


def test_unknown_expression_functions_keep_raw_text_and_explain_the_limit() -> None:
    xml = (SAMPLES_DIR / "m_member_enrollment.xml").read_bytes()
    xml = xml.replace(
        b'EXPRESSION="LTRIM(RTRIM(FIRST_NAME)) || \' \' || LTRIM(RTRIM(LAST_NAME))"',
        b'EXPRESSION="MYSTERY_FUNC(FIRST_NAME)"',
    )
    response = process_xml_request("explain", xml, {})
    assert response["status_code"] == 200
    explanation = response["content"]["explanations"]["m_member_enrollment"]
    full_name = next(
        item for item in explanation["columns"]
        if item["target_column"] == "FULL_NAME"
    )
    assert full_name["expression"] == "MYSTERY_FUNC(FIRST_NAME)"
    assert "raw expression" in full_name["rule"].lower()
    assert "MYSTERY_FUNC" in full_name["rule"]
    assert explanation["migration_status"]["blocked"] == 1
    assert any(item["severity"] == "error" for item in explanation["diagnostics"])


def test_explain_endpoint_returns_mapping_explanation_and_models() -> None:
    response = TestClient(app).post(
        "/api/explain",
        data={"sample_name": "m_member_enrollment.xml"},
    )
    assert response.status_code == 200
    explanation = response.json()["explanations"]["m_member_enrollment"]
    assert "keeping active members" in explanation["overview"]
    assert "models/staging/stg_member_enrollment.sql" in {
        model["path"] for model in explanation["generated_models"]
    }
