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
                f"SELECT COUNT(*) FROM main.stg_member_enrollment AS __in "
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
