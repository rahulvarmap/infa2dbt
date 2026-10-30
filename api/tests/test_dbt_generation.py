from __future__ import annotations

import hashlib
import json
import sqlite3
import shutil
import subprocess
import zipfile
from io import BytesIO
from pathlib import Path

import duckdb
import pytest

from api.dbt import build_project, zip_project
from api.dbt.dependency_lock import DBT_REQUIREMENTS_LOCK
from api.dbt.dialects.snowflake import (
    UnsupportedSnowflakeType,
    render_function,
    snowflake_type,
    sql_identifier,
)
from api.dbt.validation import execute_validation_workspace, prepare_validation_workspace
from api.parser import parse_powercenter_xml
from api.parser.expressions import FunctionCall, Literal, PortReference


REPO_ROOT = Path(__file__).resolve().parents[2]
SAMPLES_DIR = REPO_ROOT / "samples"
GOLDEN = Path(__file__).parent / "golden" / "m_member_enrollment.json"
CLAIMS_GOLDEN = Path(__file__).parent / "golden" / "m_claims_member_join.json"
AGGREGATOR_GOLDEN = (
    Path(__file__).parent / "golden" / "m_claims_member_summary.json"
)

SUPPORTED_XML = b"""<POWERMART><REPOSITORY NAME="R"><FOLDER NAME="F">
  <SOURCE NAME="S">
    <SOURCEFIELD NAME="A" DATATYPE="varchar2" PRECISION="20"/>
    <SOURCEFIELD NAME="B" DATATYPE="varchar2" PRECISION="20"/>
  </SOURCE>
  <TARGET NAME="T">
    <TARGETFIELD NAME="FULL_NAME" DATATYPE="varchar2" PRECISION="41"/>
    <TARGETFIELD NAME="CLEAN" DATATYPE="varchar2" PRECISION="20"/>
  </TARGET>
  <TRANSFORMATION NAME="SQ" TYPE="Source Qualifier">
    <TRANSFORMFIELD NAME="A" DATATYPE="varchar2" PRECISION="20" PORTTYPE="INPUT/OUTPUT"/>
    <TRANSFORMFIELD NAME="B" DATATYPE="varchar2" PRECISION="20" PORTTYPE="INPUT/OUTPUT"/>
  </TRANSFORMATION>
  <TRANSFORMATION NAME="EXP" TYPE="Expression">
    <TRANSFORMFIELD NAME="A" PORTTYPE="INPUT"/>
    <TRANSFORMFIELD NAME="B" PORTTYPE="INPUT"/>
    <TRANSFORMFIELD NAME="V" PORTTYPE="VARIABLE" EXPRESSION="A || ' ' || B"/>
    <TRANSFORMFIELD NAME="FULL_NAME" PORTTYPE="OUTPUT" EXPRESSION="V"/>
    <TRANSFORMFIELD NAME="CLEAN" PORTTYPE="OUTPUT"
      EXPRESSION="IIF(ISNULL(A), NVL(B, '?'), LTRIM(RTRIM(A)))"/>
  </TRANSFORMATION>
  <TRANSFORMATION NAME="FIL" TYPE="Filter">
    <TABLEATTRIBUTE NAME="Filter Condition" VALUE="FULL_NAME IS NOT NULL"/>
    <TRANSFORMFIELD NAME="FULL_NAME" PORTTYPE="INPUT/OUTPUT"/>
    <TRANSFORMFIELD NAME="CLEAN" PORTTYPE="INPUT/OUTPUT"/>
  </TRANSFORMATION>
  <MAPPING NAME="M">
    <INSTANCE NAME="S" TYPE="SOURCE" TRANSFORMATION_NAME="S" TRANSFORMATION_TYPE="Source Definition"/>
    <INSTANCE NAME="SQ" TYPE="TRANSFORMATION" TRANSFORMATION_NAME="SQ"/>
    <INSTANCE NAME="EXP" TYPE="TRANSFORMATION" TRANSFORMATION_NAME="EXP"/>
    <INSTANCE NAME="FIL" TYPE="TRANSFORMATION" TRANSFORMATION_NAME="FIL"/>
    <INSTANCE NAME="T" TYPE="TARGET" TRANSFORMATION_NAME="T" TRANSFORMATION_TYPE="Target Definition"/>
    <CONNECTOR FROMINSTANCE="S" FROMFIELD="A" TOINSTANCE="SQ" TOFIELD="A"/>
    <CONNECTOR FROMINSTANCE="S" FROMFIELD="B" TOINSTANCE="SQ" TOFIELD="B"/>
    <CONNECTOR FROMINSTANCE="SQ" FROMFIELD="A" TOINSTANCE="EXP" TOFIELD="A"/>
    <CONNECTOR FROMINSTANCE="SQ" FROMFIELD="B" TOINSTANCE="EXP" TOFIELD="B"/>
    <CONNECTOR FROMINSTANCE="EXP" FROMFIELD="FULL_NAME" TOINSTANCE="FIL" TOFIELD="FULL_NAME"/>
    <CONNECTOR FROMINSTANCE="EXP" FROMFIELD="CLEAN" TOINSTANCE="FIL" TOFIELD="CLEAN"/>
    <CONNECTOR FROMINSTANCE="FIL" FROMFIELD="FULL_NAME" TOINSTANCE="T" TOFIELD="FULL_NAME"/>
    <CONNECTOR FROMINSTANCE="FIL" FROMFIELD="CLEAN" TOINSTANCE="T" TOFIELD="CLEAN"/>
  </MAPPING>
</FOLDER></REPOSITORY></POWERMART>"""


def _project(xml: bytes = SUPPORTED_XML):
    return build_project(parse_powercenter_xml(xml))


def _with_source_qualifier_attribute(name: str, value: str) -> bytes:
    opening = b'<TRANSFORMATION NAME="SQ" TYPE="Source Qualifier">'
    addition = (
        f'<TABLEATTRIBUTE NAME="{name}" VALUE="{value}"/>'.encode()
    )
    return SUPPORTED_XML.replace(opening, opening + addition, 1)


def test_supported_subset_compiles_expression_ast_and_emits_dbt_refs():
    project = _project()
    target = project.files["models/marts/t.sql"]
    staging = project.files["models/staging/stg_s.sql"]

    assert project.blocked_models == []
    assert "{{ source(" in staging
    assert "{{ ref('stg_s') }}" in target
    assert "IFF(" in target
    assert "ISNULL(" not in target
    assert "NVL(" in target
    assert "LTRIM(RTRIM(src.a))" in target
    assert "LTRIM(RTRIM(A))" not in target
    assert "where (src.full_name IS NOT NULL)" in target
    assert "models/sources.yml" in project.files
    assert "models/schema.yml" in project.files


@pytest.mark.parametrize(("attribute", "value", "code"), [
    ("SQL Query", "select A from S", "SOURCE_QUALIFIER_SQL_OVERRIDE"),
    ("User Defined Join", "S.A = X.A", "SOURCE_QUALIFIER_USER_DEFINED_JOIN"),
])
def test_source_qualifier_unsupported_features_block_target(
    attribute: str,
    value: str,
    code: str,
):
    project = _project(_with_source_qualifier_attribute(attribute, value))
    model = project.files["models/marts/t.sql"]
    assert model.startswith("-- BLOCKED MODEL: t")
    assert any(item["code"] == code for item in project.diagnostics)
    assert project.blocked_models[0]["reasons"] == [code]


def test_source_qualifier_filter_is_translated_in_mapping_cte():
    project = _project(_with_source_qualifier_attribute(
        "Source Filter",
        "S.A IN ('x', 'y') AND B NOT LIKE 'z%'",
    ))
    staging = project.files["models/staging/stg_s.sql"]
    target = project.files["models/marts/t.sql"]

    assert project.blocked_models == []
    assert "where" not in staging.lower()
    assert "where ((src.a IN ('x', 'y')) AND (src.b NOT LIKE 'z%'))" in target
    assert "{{ ref('stg_s') }}" in target


@pytest.mark.parametrize(("value", "code"), [
    ("A =", "SOURCE_QUALIFIER_SOURCE_FILTER_PARSE"),
    ("UPPER(A) = 'X'", "SOURCE_QUALIFIER_SOURCE_FILTER_PARSE"),
    ("UNKNOWN_COLUMN IS NULL", "SOURCE_QUALIFIER_SOURCE_FILTER_COLUMN"),
])
def test_invalid_source_qualifier_filter_blocks_target_with_diagnostic(value, code):
    project = _project(_with_source_qualifier_attribute("Source Filter", value))

    assert project.files["models/marts/t.sql"].startswith("-- BLOCKED MODEL: t")
    assert project.blocked_models[0]["reasons"] == [code]
    assert any(
        item["code"] == code
        and item["mapping"] == "M"
        and item["instance"] == "SQ"
        and item["field"] == "Source Filter"
        for item in project.diagnostics
    )


def test_unknown_function_is_diagnostic_and_blocks_its_target():
    xml = SUPPORTED_XML.replace(b"IIF(ISNULL(A)", b"VENDOR_FUNC(ISNULL(A)")
    project = _project(xml)
    assert project.files["models/marts/t.sql"].startswith("-- BLOCKED MODEL:")
    assert any(item["code"] == "UNKNOWN_EXPRESSION_FUNCTION" for item in project.diagnostics)
    assert any(item["code"] == "UNSUPPORTED_EXPRESSION_FUNCTION" for item in project.diagnostics)


def test_unsupported_transformation_marks_target_placeholder():
    xml = SUPPORTED_XML.replace(
        b'<TRANSFORMATION NAME="FIL" TYPE="Filter">',
        b'<TRANSFORMATION NAME="FIL" TYPE="Vendor Custom">',
        1,
    )
    project = _project(xml)
    assert project.files["models/marts/t.sql"].startswith("-- BLOCKED MODEL:")
    assert "UNSUPPORTED_TRANSFORMATION" in project.blocked_models[0]["reasons"]


@pytest.mark.parametrize(("datatype", "precision", "scale", "expected"), [
    ("varchar2", "20", "0", "VARCHAR(20)"),
    ("char", "1", "0", "VARCHAR(1)"),
    ("number", "12", "2", "NUMBER(12, 2)"),
    ("integer", "10", "0", "NUMBER(10, 0)"),
    ("date", "19", "0", "TIMESTAMP_NTZ"),
    ("double", "", "", "FLOAT"),
    ("boolean", "", "", "BOOLEAN"),
])
def test_snowflake_datatype_translations(datatype, precision, scale, expected):
    assert snowflake_type(datatype, precision, scale) == expected


def test_unsupported_snowflake_datatype_fails_explicitly():
    with pytest.raises(UnsupportedSnowflakeType):
        snowflake_type("unmapped_type")


def test_snowflake_function_translation_is_centralized_and_checks_arity():
    assert render_function(FunctionCall("TO_CHAR", ()), ["value", "'YYYY'"]) == (
        "TO_VARCHAR(value, 'YYYY')"
    )
    with pytest.raises(ValueError, match="IIF requires"):
        render_function(FunctionCall("IIF", ()), ["only-one"])


def test_sample_date_functions_have_centralized_snowflake_translations():
    date_diff = FunctionCall(
        "DATE_DIFF",
        (
            FunctionCall("SYSDATE", ()),
            PortReference("DOB"),
            Literal("string", "YYYY"),
        ),
    )
    assert render_function(
        date_diff,
        ["CURRENT_TIMESTAMP()::TIMESTAMP_NTZ", '"dob"', "'YYYY'"],
    ) == "(MONTHS_BETWEEN(CURRENT_TIMESTAMP()::TIMESTAMP_NTZ, \"dob\") / 12)"
    month_diff = FunctionCall(
        "DATE_DIFF",
        (
            FunctionCall("SYSDATE", ()),
            PortReference("DOB"),
            Literal("string", "MM"),
        ),
    )
    assert render_function(
        month_diff,
        ["CURRENT_TIMESTAMP()::TIMESTAMP_NTZ", '"dob"', "'MM'"],
    ) == "MONTHS_BETWEEN(CURRENT_TIMESTAMP()::TIMESTAMP_NTZ, \"dob\")"
    assert render_function(
        FunctionCall("TRUNC", (FunctionCall("SYSDATE", ()),)),
        ["CURRENT_TIMESTAMP()::TIMESTAMP_NTZ"],
    ) == "DATE_TRUNC('DAY', CURRENT_TIMESTAMP()::TIMESTAMP_NTZ)"
    assert render_function(
        FunctionCall("TRUNC", (date_diff,)),
        ["(MONTHS_BETWEEN(CURRENT_TIMESTAMP()::TIMESTAMP_NTZ, \"dob\") / 12)"],
    ) == "TRUNC((MONTHS_BETWEEN(CURRENT_TIMESTAMP()::TIMESTAMP_NTZ, \"dob\") / 12))"


def test_sql_identifier_quotes_only_reserved_or_invalid_names():
    assert sql_identifier("member_id") == "member_id"
    assert sql_identifier("select") == '"select"'
    assert sql_identifier("member id") == '"member id"'
    assert sql_identifier("account") == "account"
    assert sql_identifier("left") == "left"
    assert sql_identifier("left", context="relation") == '"left"'
    assert sql_identifier("case") == '"case"'


def test_generated_target_query_uses_unquoted_column_access():
    target = _project().files["models/marts/t.sql"]
    assert "sq as (" in target
    assert "src.a as a" in target
    assert '"src"."a"' not in target
    query = target.replace(
        "{{ ref('stg_s') }}",
        "(select 'Ada' as a, 'Lovelace' as b)",
    ).split("\n\n", 1)[1]

    connection = sqlite3.connect(":memory:")
    connection.create_function(
        "IFF", 3, lambda condition, truthy, falsy: truthy if condition else falsy
    )
    connection.create_function("NVL", 2, lambda value, fallback: fallback if value is None else value)
    try:
        assert connection.execute(query).fetchone() == ("Ada", "Ada Lovelace")
    finally:
        connection.close()


def test_project_zip_is_byte_for_byte_deterministic():
    project = _project()
    first = zip_project(project.files)
    second = zip_project(project.files)
    assert first == second
    with zipfile.ZipFile(BytesIO(first)) as archive:
        assert archive.namelist() == sorted(project.files)
        assert all(item.date_time == (1980, 1, 1, 0, 0, 0) for item in archive.infolist())
        assert hashlib.sha256(first).hexdigest() == hashlib.sha256(second).hexdigest()


def test_sample_project_matches_golden_files():
    sample = parse_powercenter_xml((SAMPLES_DIR / "m_member_enrollment.xml").read_bytes())
    expected = json.loads(GOLDEN.read_text())
    files = build_project(sample).files
    assert set(files) == set(expected) | {"requirements.lock"}
    assert {path: content for path, content in files.items() if path != "requirements.lock"} == expected
    assert files["requirements.lock"] == DBT_REQUIREMENTS_LOCK


def test_claims_member_sample_project_matches_golden_files():
    sample = parse_powercenter_xml(
        (SAMPLES_DIR / "m_claims_member_join.xml").read_bytes()
    )
    expected = json.loads(CLAIMS_GOLDEN.read_text())
    files = build_project(sample).files
    assert set(files) == set(expected) | {"requirements.lock"}
    assert {
        path: content for path, content in files.items()
        if path != "requirements.lock"
    } == expected
    assert files["requirements.lock"] == DBT_REQUIREMENTS_LOCK


def test_claims_provider_lookup_sample_project_matches_golden_files():
    sample = parse_powercenter_xml(
        (SAMPLES_DIR / "m_claims_provider_lookup.xml").read_bytes()
    )
    expected = json.loads(
        (Path(__file__).parent / "golden" / "m_claims_provider_lookup.json").read_text()
    )
    files = build_project(sample).files
    assert set(files) == set(expected) | {"requirements.lock"}
    assert {
        path: content for path, content in files.items()
        if path != "requirements.lock"
    } == expected
    assert files["requirements.lock"] == DBT_REQUIREMENTS_LOCK


def test_claims_member_summary_sample_project_matches_golden_files():
    sample = parse_powercenter_xml(
        (SAMPLES_DIR / "m_claims_member_summary.xml").read_bytes()
    )
    expected = json.loads(AGGREGATOR_GOLDEN.read_text())
    project = build_project(sample)
    assert set(project.files) == set(expected) | {"requirements.lock"}
    assert {
        path: content for path, content in project.files.items()
        if path != "requirements.lock"
    } == expected
    assert project.files["requirements.lock"] == DBT_REQUIREMENTS_LOCK
    target_sql = project.files["models/marts/member_claim_summary.sql"]
    assert (
        "SUM(CASE WHEN (src.claim_status = 'PAID') THEN src.claim_amount END)"
        in target_sql
    )
    assert "group by src.member_id" in target_sql
    assert "ANY_VALUE(src.claim_status)" in target_sql
    assert any(
        item["code"] == "AGGREGATOR_ANY_VALUE_SEMANTICS"
        and item["severity"] == "warning"
        for item in project.diagnostics
    )
    assert any(
        item["code"] == "AGGREGATOR_SORTED_INPUT_IGNORED"
        and item["severity"] == "info"
        for item in project.diagnostics
    )


@pytest.mark.parametrize(
    ("expression", "diagnostic_code"),
    [
        ("SUM(AVG(CLAIM_AMOUNT))", "NESTED_AGGREGATE_UNSUPPORTED"),
        (
            "SUM(UNSUPPORTED_FUNC(CLAIM_AMOUNT))",
            "UNSUPPORTED_AGGREGATE_FUNCTION",
        ),
    ],
)
def test_aggregator_unsupported_expressions_block_with_diagnostics(
    expression: str,
    diagnostic_code: str,
):
    xml = (SAMPLES_DIR / "m_claims_member_summary.xml").read_bytes().replace(
        b"SUM(CLAIM_AMOUNT)",
        expression.encode(),
        1,
    )
    project = build_project(parse_powercenter_xml(xml))
    assert project.files["models/marts/member_claim_summary.sql"].startswith(
        "-- BLOCKED MODEL:"
    )
    assert diagnostic_code in project.blocked_models[0]["reasons"]
    assert any(
        item["code"] == diagnostic_code and item["severity"] == "error"
        for item in project.diagnostics
    )


def test_lookup_sql_override_and_unconnected_lookup_are_blocked():
    sample_path = SAMPLES_DIR / "m_claims_provider_lookup.xml"
    xml = sample_path.read_bytes()
    for updated, expected_code in [
        (
            xml.replace(
                b'NAME="Lookup SQL Override" VALUE=""',
                b'NAME="Lookup SQL Override" VALUE="select * from PROVIDER"',
            ),
            "LOOKUP_SQL_OVERRIDE_UNSUPPORTED",
        ),
        (
            xml.replace(
                b'<TABLEATTRIBUTE NAME="Lookup table name" VALUE="PROVIDER"/>',
                b'<TABLEATTRIBUTE NAME="Lookup table name" VALUE="PROVIDER"/>'
                b'<TABLEATTRIBUTE NAME="Lookup Type" VALUE="Unconnected"/>',
            ),
            "UNCONNECTED_LOOKUP_UNSUPPORTED",
        ),
    ]:
        project = build_project(parse_powercenter_xml(updated))
        assert project.files["models/marts/fact_claim_provider.sql"].startswith(
            "-- BLOCKED MODEL:"
        )
        assert expected_code in project.blocked_models[0]["reasons"]
        assert any(item["code"] == expected_code for item in project.diagnostics)


@pytest.mark.parametrize(
    ("policy", "expected_duplicate"),
    [
        ("Use First Value", "Provider First"),
        ("Use Last Value", "Provider Second"),
        ("Use Any Value", "Provider First"),
    ],
)
def test_connected_lookup_match_policies_are_deterministic(
    policy: str,
    expected_duplicate: str,
    tmp_path: Path,
):
    xml = (SAMPLES_DIR / "m_claims_provider_lookup.xml").read_bytes().replace(
        b'NAME="Lookup Policy on Multiple Match" VALUE="Use First Value"',
        f'NAME="Lookup Policy on Multiple Match" VALUE="{policy}"'.encode(),
    )
    parsed = parse_powercenter_xml(xml)
    snowflake = build_project(parsed, dialect="snowflake")
    assert not snowflake.blocked_models
    target_sql = snowflake.files["models/marts/fact_claim_provider.sql"]
    assert "QUALIFY ROW_NUMBER() OVER" in target_sql
    direction = (
        "DESC NULLS LAST" if policy == "Use Last Value" else "ASC NULLS FIRST"
    )
    assert (
        "ORDER BY "
        f"lookup_candidate.provider_name {direction}, "
        f"lookup_candidate.provider_tier {direction}, "
        f"lookup_candidate.provider_id {direction}"
    ) in target_sql
    assert "CASE WHEN lkp.lkp_matched IS NULL THEN 'UNKNOWN PROVIDER'" in target_sql
    assert "COALESCE(lkp.provider_name" not in target_sql

    workspace = tmp_path / policy.lower().replace(" ", "_")
    project = prepare_validation_workspace(parsed, workspace)
    assert "ROW_NUMBER() OVER" in project.files[
        "models/marts/fact_claim_provider.sql"
    ]
    report = execute_validation_workspace(workspace, project)
    assert report["status"] == "passed", report
    connection = duckdb.connect(str(workspace / "validation.duckdb"))
    try:
        rows = connection.execute(
            "SELECT claim_id, provider_name, provider_tier "
            "FROM main.fact_claim_provider ORDER BY claim_id"
        ).fetchall()
    finally:
        connection.close()
    assert rows == [
        ("C101", "Alpha Clinic", "A"),
        ("C102", expected_duplicate, "B" if policy != "Use Last Value" else "C"),
        ("C103", "Gamma Clinic", None),
        ("C104", "UNKNOWN PROVIDER", "UNASSIGNED"),
        ("C105", expected_duplicate, "B" if policy != "Use Last Value" else "C"),
        ("C106", "UNKNOWN PROVIDER", "UNASSIGNED"),
    ]


def test_lookup_report_error_and_unsupported_policies_block_with_diagnostics():
    xml = (SAMPLES_DIR / "m_claims_provider_lookup.xml").read_bytes()
    for policy, code in [
        ("Report Error", "LOOKUP_REPORT_ERROR_POLICY"),
        ("Use Random Value", "UNSUPPORTED_LOOKUP_POLICY"),
    ]:
        updated = xml.replace(
            b'NAME="Lookup Policy on Multiple Match" VALUE="Use First Value"',
            f'NAME="Lookup Policy on Multiple Match" VALUE="{policy}"'.encode(),
        )
        project = build_project(parse_powercenter_xml(updated))
        assert code in project.blocked_models[0]["reasons"]
        assert any(item["code"] == code for item in project.diagnostics)


def test_lookup_non_equi_condition_is_blocked_with_diagnostic():
    xml = (SAMPLES_DIR / "m_claims_provider_lookup.xml").read_bytes().replace(
        b"INPUT.PROVIDER_ID = LOOKUP.LKP_PROVIDER_ID",
        b"INPUT.PROVIDER_ID &lt; LOOKUP.LKP_PROVIDER_ID",
    )
    project = build_project(parse_powercenter_xml(xml))
    assert project.files["models/marts/fact_claim_provider.sql"].startswith(
        "-- BLOCKED MODEL:"
    )
    assert "NON_EQUI_LOOKUP_CONDITION" in project.blocked_models[0]["reasons"]
    assert any(
        item["code"] == "NON_EQUI_LOOKUP_CONDITION"
        and item["instance"] == "LKP_PROVIDER"
        and item["field"] == "Lookup Condition"
        for item in project.diagnostics
    )


@pytest.mark.parametrize(
    "sample_path",
    sorted(SAMPLES_DIR.glob("*.xml")),
    ids=lambda path: path.name,
)
def test_every_bundled_sample_passes_dbt_parse(sample_path: Path, tmp_path: Path):
    executable = shutil.which("dbt")
    assert executable, "dbt must be installed from the pinned Python requirements."
    project = build_project(parse_powercenter_xml(sample_path.read_bytes()))
    for relative_path, content in project.files.items():
        destination = tmp_path / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content)
    parsed = subprocess.run(
        [executable, "parse", "--project-dir", str(tmp_path), "--profiles-dir", str(tmp_path)],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert parsed.returncode == 0, parsed.stdout + parsed.stderr
