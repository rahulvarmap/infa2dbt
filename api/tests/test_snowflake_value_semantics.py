import calendar
from datetime import date
import math
import sqlite3

import pytest

from api.dbt.dialects.snowflake import render_binary, render_function
from api.parser.expressions import FunctionCall, Literal, PortReference


def _months_between(left: str, right: str) -> float:
    first = date.fromisoformat(left[:10])
    second = date.fromisoformat(right[:10])
    whole_months = (
        (first.year - second.year) * 12 + first.month - second.month
    )
    first_is_month_end = first.day == calendar.monthrange(first.year, first.month)[1]
    second_is_month_end = (
        second.day == calendar.monthrange(second.year, second.month)[1]
    )
    if first.day == second.day or (first_is_month_end and second_is_month_end):
        return float(whole_months)
    return whole_months + (first.day - second.day) / 31


def _evaluate_sqlite(expression: str):
    connection = sqlite3.connect(":memory:")
    connection.create_function("MONTHS_BETWEEN", 2, _months_between)
    connection.create_function("TRUNC", 1, math.trunc)
    try:
        return connection.execute(f"select {expression}").fetchone()[0]
    finally:
        connection.close()


def _date_diff_sql(first: str, second: str, unit: str) -> str:
    function = FunctionCall(
        "DATE_DIFF",
        (
            PortReference("D1"),
            PortReference("D2"),
            Literal("string", unit),
        ),
    )
    return render_function(
        function,
        [f"'{first}'", f"'{second}'", f"'{unit}'"],
    )


@pytest.mark.parametrize(
    ("today", "expected_age"),
    [
        ("2025-02-28", 24),  # the day before a March 1 birthday
        ("2025-03-01", 25),  # the birthday
    ],
)
def test_truncated_year_date_diff_matches_age_before_and_on_birthday(today, expected_age):
    date_diff = FunctionCall(
        "DATE_DIFF",
        (
            PortReference("D1"),
            PortReference("D2"),
            Literal("string", "YYYY"),
        ),
    )
    difference_sql = _date_diff_sql(today, "2000-03-01", "YYYY")
    age_sql = render_function(
        FunctionCall("TRUNC", (date_diff,)),
        [difference_sql],
    )

    assert "MONTHS_BETWEEN(" in difference_sql
    assert "/ 12" in difference_sql
    assert _evaluate_sqlite(age_sql) == expected_age


def test_month_date_diff_uses_fractional_months_and_preserves_sign():
    expected = 1 + 14 / 31
    forward = _date_diff_sql("2025-03-15", "2025-02-01", "MM")
    reverse = _date_diff_sql("2025-02-01", "2025-03-15", "MM")

    assert forward.startswith("MONTHS_BETWEEN(")
    assert _evaluate_sqlite(forward) == pytest.approx(expected)
    assert _evaluate_sqlite(reverse) == pytest.approx(-expected)


@pytest.mark.parametrize(
    ("kind", "operands", "expected"),
    [
        ("operator", ["NULL", "'member'"], "member"),
        ("operator", ["NULL", "NULL"], None),
        ("function", ["NULL", "'member'", "' name'"], "member name"),
        ("function", ["NULL", "NULL"], None),
    ],
)
def test_concatenation_ignores_null_operands_except_when_all_are_null(
    kind, operands, expected
):
    if kind == "operator":
        sql = render_binary("||", operands[0], operands[1])
    else:
        function = FunctionCall(
            "CONCAT",
            tuple(Literal("null", None) for _ in operands),
        )
        sql = render_function(function, operands)

    assert _evaluate_sqlite(sql) == expected
