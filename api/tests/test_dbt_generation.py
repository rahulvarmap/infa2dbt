from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import zipfile
from io import BytesIO
from pathlib import Path

import pytest

from api.dbt import build_project, zip_project
from api.dbt.dependency_lock import DBT_REQUIREMENTS_LOCK
from api.dbt.dialects.snowflake import (
    UnsupportedSnowflakeType,
    render_function,
    snowflake_type,
)
from api.parser import parse_powercenter_xml
from api.parser.expressions import FunctionCall, Literal, PortReference


REPO_ROOT = Path(__file__).resolve().parents[2]
SAMPLES_DIR = REPO_ROOT / "samples"
GOLDEN = Path(__file__).parent / "golden" / "m_member_enrollment.json"

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
    assert 'LTRIM(RTRIM("__in"."a"))' in target
    assert "LTRIM(RTRIM(A))" not in target
    assert "where (\"__in\".\"full_name\" IS NOT NULL)" in target
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


def test_source_qualifier_source_filter_is_translated_in_staging_model():
    project = _project(_with_source_qualifier_attribute(
        "Source Filter",
        "S.A IN ('x', 'y') AND B NOT LIKE 'z%'",
    ))
    staging = project.files["models/staging/stg_s.sql"]

    assert project.blocked_models == []
    assert "where ((src.\"A\" IN ('x', 'y')) AND (src.\"B\" NOT LIKE 'z%'))" in staging
    assert "{{ ref('stg_s') }}" in project.files["models/marts/t.sql"]


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
        b'<TRANSFORMATION NAME="FIL" TYPE="Joiner">',
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
    ) == "DATEDIFF(year, \"dob\", CURRENT_TIMESTAMP()::TIMESTAMP_NTZ)"
    assert render_function(
        FunctionCall("TRUNC", (FunctionCall("SYSDATE", ()),)),
        ["CURRENT_TIMESTAMP()::TIMESTAMP_NTZ"],
    ) == "DATE_TRUNC('DAY', CURRENT_TIMESTAMP()::TIMESTAMP_NTZ)"
    assert render_function(
        FunctionCall("TRUNC", (date_diff,)),
        ["DATEDIFF(year, \"dob\", CURRENT_TIMESTAMP()::TIMESTAMP_NTZ)"],
    ) == "TRUNC(DATEDIFF(year, \"dob\", CURRENT_TIMESTAMP()::TIMESTAMP_NTZ))"


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
