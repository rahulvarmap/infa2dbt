from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

import duckdb
import pytest

from api.dbt.dialects import duckdb as duckdb_dialect
from api.dbt.generator import build_project
from api.dbt.validation import execute_validation_workspace, prepare_validation_workspace
from api.parser.expressions import FunctionCall, Literal
from api.parser.parser import parse_powercenter_xml


def _value(connection: duckdb.DuckDBPyConnection, expression: str):
    return connection.execute(f"SELECT {expression}").fetchone()[0]


def _date_diff_ast(unit: str) -> FunctionCall:
    return FunctionCall(
        "DATE_DIFF",
        (
            Literal("string", "first_date"),
            Literal("string", "second_date"),
            Literal("string", unit),
        ),
    )


def test_duckdb_function_golden_values() -> None:
    connection = duckdb.connect(":memory:")
    try:
        iif = duckdb_dialect.render_function(
            FunctionCall("IIF", (Literal("boolean", True), Literal("string", "yes"), Literal("string", "no"))),
            ["TRUE", "'yes'", "'no'"],
        )
        assert _value(connection, iif) == "yes"
        assert _value(connection, iif.replace("TRUE", "FALSE")) == "no"

        decode = duckdb_dialect.render_function(FunctionCall("DECODE", ()), [
            "NULL", "NULL", "'matched null'", "'fallback'",
        ])
        assert _value(connection, decode) == "matched null"
        decode_default = duckdb_dialect.render_function(FunctionCall("DECODE", ()), [
            "'X'", "'M'", "'Male'", "'fallback'",
        ])
        assert _value(connection, decode_default) == "fallback"

        nvl = duckdb_dialect.render_function(FunctionCall("NVL", ()), ["NULL", "'fallback'"])
        assert _value(connection, nvl) == "fallback"
        assert _value(
            connection,
            duckdb_dialect.render_function(FunctionCall("NVL", ()), ["'value'", "'fallback'"]),
        ) == "value"

        trunc = duckdb_dialect.render_function(
            FunctionCall("TRUNC", (FunctionCall("SYSDATE", ()),)),
            ["TIMESTAMP '2025-03-01 14:35:21'"],
        )
        assert _value(connection, trunc) == datetime(2025, 3, 1)
    finally:
        connection.close()


def test_duckdb_date_diff_sign_argument_order_and_birthday_boundaries() -> None:
    connection = duckdb.connect(":memory:")
    try:
        month_ast = _date_diff_ast("MM")
        positive_months = duckdb_dialect.render_function(month_ast, [
            "DATE '2025-03-16'", "DATE '2025-02-01'", "'MM'",
        ])
        negative_months = duckdb_dialect.render_function(month_ast, [
            "DATE '2025-02-01'", "DATE '2025-03-16'", "'MM'",
        ])
        assert _value(connection, positive_months) == pytest.approx(1 + 15 / 31)
        assert _value(connection, negative_months) == pytest.approx(-(1 + 15 / 31))

        year_ast = _date_diff_ast("YYYY")
        day_before = duckdb_dialect.render_function(
            FunctionCall("TRUNC", (year_ast,)),
            [duckdb_dialect.render_function(year_ast, [
                "TIMESTAMP '2025-02-28 12:00:00'",
                "DATE '2000-03-01'",
                "'YYYY'",
            ])],
        )
        birthday = duckdb_dialect.render_function(
            FunctionCall("TRUNC", (year_ast,)),
            [duckdb_dialect.render_function(year_ast, [
                "TIMESTAMP '2025-03-01 12:00:00'",
                "DATE '2000-03-01'",
                "'YYYY'",
            ])],
        )
        assert _value(connection, day_before) == 24
        assert _value(connection, birthday) == 25
    finally:
        connection.close()


def test_duckdb_concat_null_semantics_and_unquoted_column_access() -> None:
    connection = duckdb.connect(":memory:")
    try:
        assert _value(connection, duckdb_dialect.render_concat(["NULL", "'right'"])) == "right"
        assert _value(connection, duckdb_dialect.render_concat(["NULL", "NULL"])) is None
        assert _value(connection, duckdb_dialect.render_binary("||", "NULL", "'right'")) == "right"
        assert _value(connection, duckdb_dialect.render_binary("||", "NULL", "NULL")) is None

        column = duckdb_dialect.sql_identifier("full_name")
        assert column == "full_name"
        assert connection.execute(
            f"SELECT {column} FROM (SELECT 'Ada Lovelace' AS full_name)"
        ).fetchone()[0] == "Ada Lovelace"
    finally:
        connection.close()


def test_all_bundled_samples_compile_run_and_match_golden_values(tmp_path: Path) -> None:
    samples = sorted(Path("samples").glob("*.xml"))
    assert samples, "Expected at least one bundled PowerCenter sample."

    for sample_path in samples:
        parsed = parse_powercenter_xml(sample_path.read_bytes())
        snowflake_project = build_project(parsed)
        assert snowflake_project.files["dbt_project.yml"].startswith(
            "name: infa2dbt_snowflake\n"
        )

        workspace = tmp_path / sample_path.stem
        project = prepare_validation_workspace(parsed, workspace)
        report = execute_validation_workspace(workspace, project)
        assert report["status"] == "passed", report
        assert [step["status"] for step in report["steps"]] == [
            "passed", "passed", "passed",
        ]
        assert all(model["status"] == "passed" for model in report["models"]), report

        if sample_path.name == "m_claims_member_join.xml":
            row_counts = {
                model["name"]: model["row_count"] for model in report["models"]
            }
            assert row_counts == {
                "fact_claim": 4,
                "stg_claims": 6,
                "stg_member": 6,
            }
            continue
        if sample_path.name == "m_claims_provider_lookup.xml":
            row_counts = {
                model["name"]: model["row_count"] for model in report["models"]
            }
            assert row_counts == {
                "fact_claim_provider": 6,
                "stg_claims": 6,
                "stg_provider": 6,
            }
            connection = duckdb.connect(str(workspace / "validation.duckdb"))
            try:
                rows = connection.execute(
                    "SELECT claim_id, provider_id, provider_name, provider_tier "
                    "FROM main.fact_claim_provider ORDER BY claim_id"
                ).fetchall()
            finally:
                connection.close()
            assert rows == [
                ("C101", "P001", "Alpha Clinic", "A"),
                ("C102", "P002", "Provider First", "B"),
                ("C103", "P003", "Gamma Clinic", "A"),
                ("C104", "P404", "UNKNOWN PROVIDER", "UNASSIGNED"),
                ("C105", "P002", "Provider First", "B"),
                ("C106", None, "UNKNOWN PROVIDER", "UNASSIGNED"),
            ]
            continue
        if sample_path.name != "m_member_enrollment.xml":
            continue
        target_path = next(
            path for path in project.files
            if path.startswith("models/") and path.endswith("/dim_member.sql")
        )
        target_sql = project.files[target_path]
        source_filter = re.search(
            r"sq_member_enrollment as \(.*?\n  where (.*?)\n\),\nexp_member_derive",
            target_sql,
            re.DOTALL,
        )
        assert source_filter, "Expected the generated SQ CTE to contain its filter."
        stage_sql = project.files["models/staging/stg_member_enrollment.sql"]
        assert "where" not in stage_sql.lower()

        connection = duckdb.connect(str(workspace / "validation.duckdb"))
        try:
            assert connection.execute(
                f"SELECT COUNT(*) FROM main.stg_member_enrollment AS src "
                f"WHERE {source_filter.group(1)}"
            ).fetchone()[0] == 5
            rows = connection.execute(
                "SELECT member_id, age, full_name, gender_desc "
                "FROM main.dim_member ORDER BY member_id"
            ).fetchall()
        finally:
            connection.close()
        assert rows == [
            ("SYN-001", 25.0, "Ada Lovelace", "Male"),
            ("SYN-002", 24.0, "Grace Hopper", "Female"),
            ("SYN-003", 25.0, " NullFirst", "Male"),
            ("SYN-004", 25.0, " ", "Unknown"),
        ]


@pytest.mark.parametrize(
    ("join_type", "expected_count"),
    [
        ("Normal", 4),
        ("Master Outer", 6),
        ("Detail Outer", 7),
        ("Full Outer", 9),
    ],
)
def test_joiner_types_preserve_expected_rows_and_nulls(
    join_type: str,
    expected_count: int,
    tmp_path: Path,
) -> None:
    sample_path = Path("samples/m_claims_member_join.xml")
    xml = sample_path.read_bytes().replace(
        b'NAME="Join Type" VALUE="Normal"',
        f'NAME="Join Type" VALUE="{join_type}"'.encode(),
    )
    parsed = parse_powercenter_xml(xml)
    snowflake_project = build_project(parsed)
    expected_join_sql = {
        "Normal": "INNER JOIN",
        "Master Outer": "RIGHT OUTER JOIN",
        "Detail Outer": "LEFT OUTER JOIN",
        "Full Outer": "FULL OUTER JOIN",
    }[join_type]
    assert not snowflake_project.blocked_models
    assert expected_join_sql in snowflake_project.files["models/marts/fact_claim.sql"]
    project = prepare_validation_workspace(
        parsed,
        tmp_path / join_type.lower().replace(" ", "_"),
    )
    target_sql = project.files["models/marts/fact_claim.sql"].split("\n\n", 1)[1]
    for seed in project.seed_tables:
        stage_name = seed.identifier.removeprefix("seed_")
        target_sql = target_sql.replace(
            f"{{{{ ref('{stage_name}') }}}}",
            f"main.{seed.identifier}",
        )

    connection = duckdb.connect(str(tmp_path / join_type.lower().replace(" ", "_") / "validation.duckdb"))
    try:
        rows = connection.execute(
            "SELECT claim_id, member_id, claim_member_id, claim_amount, member_name "
            f"FROM ({target_sql}) AS result "
            "ORDER BY claim_id NULLS LAST, member_id NULLS LAST"
        ).fetchall()
    finally:
        connection.close()

    assert len(rows) == expected_count
    if join_type == "Normal":
        assert rows == [
            ("C001", "M001", "M001", "100", "Ada"),
            ("C002", "M002", "M002", "200", "Grace"),
            ("C003", "M002", "M002", "75", "Grace"),
            ("C004", "M004", "M004", "300", "Alan"),
        ]
    elif join_type == "Master Outer":
        unmatched_claims = {row[0]: row for row in rows if row[0] in {"C005", "C006"}}
        assert unmatched_claims["C005"] == ("C005", None, "M999", "400", None)
        assert unmatched_claims["C006"] == ("C006", None, None, "500", None)
    elif join_type == "Detail Outer":
        unmatched_members = [row for row in rows if row[0] is None]
        assert {row[1] for row in unmatched_members} == {"M003", "M005", None}
        assert all(row[2] is None and row[3] is None for row in unmatched_members)
    else:
        assert any(row[0] == "C005" and row[1] is None for row in rows)
        assert any(row[0] is None and row[1] == "M003" for row in rows)
        assert any(
            row[0] == "C005" and row[1] is None and row[2] == "M999"
            and row[3] == "400" and row[4] is None
            for row in rows
        )
        assert any(
            row[0] is None and row[1] == "M003" and row[2] is None
            and row[3] is None and row[4] == "Katherine"
            for row in rows
        )


def test_joiner_supports_multiple_equality_conditions_and_blocks_non_equi(
    tmp_path: Path,
) -> None:
    xml = Path("samples/m_claims_member_join.xml").read_bytes()
    condition = b"MASTER.MEMBER_ID = DETAIL.CLAIM_MEMBER_ID"
    multi_condition = (
        b"MASTER.MEMBER_ID = DETAIL.CLAIM_MEMBER_ID AND "
        b"MASTER.MEMBER_NAME = DETAIL.CLAIM_ID"
    )
    parsed_multiple = parse_powercenter_xml(xml.replace(condition, multi_condition))
    project = prepare_validation_workspace(parsed_multiple, tmp_path / "multi_condition")
    sql = project.files["models/marts/fact_claim.sql"]
    assert not project.blocked_models
    assert (
        " on mst.member_id = dtl.member_id AND "
        "mst.member_name = dtl.claim_id"
    ) in sql
    target_sql = sql.split("\n\n", 1)[1]
    for seed in project.seed_tables:
        stage_name = seed.identifier.removeprefix("seed_")
        target_sql = target_sql.replace(
            f"{{{{ ref('{stage_name}') }}}}",
            f"main.{seed.identifier}",
        )
    connection = duckdb.connect(
        str(tmp_path / "multi_condition" / "validation.duckdb")
    )
    try:
        assert connection.execute(
            f"SELECT * FROM ({target_sql}) AS result"
        ).fetchall() == []
    finally:
        connection.close()

    non_equi_xml = xml.replace(
        condition,
        b"MASTER.MEMBER_ID &lt; DETAIL.CLAIM_MEMBER_ID",
    )
    blocked = build_project(
        parse_powercenter_xml(non_equi_xml),
        dialect="snowflake",
    )
    fact_model = next(
        model for model in blocked.blocked_models
        if model["path"] == "models/marts/fact_claim.sql"
    )
    assert "NON_EQUI_JOIN_CONDITION" in fact_model["reasons"]
    assert any(
        diagnostic["code"] == "NON_EQUI_JOIN_CONDITION"
        and diagnostic["mapping"] == "m_claims_member_join"
        and diagnostic["instance"] == "JNR_CLAIM_MEMBER"
        for diagnostic in blocked.diagnostics
    )
