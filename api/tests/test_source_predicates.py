import pytest

from api.dbt.dialects.snowflake import render_predicate, sql_identifier
from api.dbt.predicates import parse_simple_predicate
from api.parser.expressions import (
    BinaryOperator,
    ExpressionSyntaxError,
    ListExpression,
    PortReference,
    parse_expression,
)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("S.A = 'x'", "(src.A = 'x')"),
        ("(A >= 2 AND B LIKE 'x%')", "((src.A >= 2) AND (src.B LIKE 'x%'))"),
        ("A IN ('x', 'y')", "(src.A IN ('x', 'y'))"),
        ("B NOT IN (1, 2)", "(src.B NOT IN (1, 2))"),
        ("A IS NULL", "(src.A IS NULL)"),
        ("A IS NOT NULL", "(src.A IS NOT NULL)"),
        ("A NOT LIKE 'z%'", "(src.A NOT LIKE 'z%')"),
        (
            "NOT (A = 'x' OR B < 5)",
            "(NOT ((src.A = 'x') OR (src.B < 5)))",
        ),
    ],
)
def test_source_filter_predicates_translate_to_snowflake(source, expected):
    predicate = parse_simple_predicate(source)

    def resolve_column(reference):
        return (
            f"src.{sql_identifier(reference.name.split('.')[-1])}"
        )

    assert render_predicate(predicate, resolve_column) == expected


@pytest.mark.parametrize(
    "source",
    [
        "",
        "A =",
        "A IN ()",
        "A IN ('x',)",
        "A + 1 > 2",
        "UPPER(A) = 'X'",
        "TRUE",
        "A = 1 OR",
    ],
)
def test_source_filter_parser_rejects_unsupported_or_malformed_predicates(source):
    with pytest.raises(ExpressionSyntaxError):
        parse_simple_predicate(source)


def test_expression_parser_preserves_qualified_column_refs_and_in_lists():
    is_not_null = parse_expression("MEMBER_ENROLLMENT.STATUS_CD IS NOT NULL")
    assert isinstance(is_not_null, BinaryOperator)
    assert isinstance(is_not_null.left, PortReference)
    assert is_not_null.left.name == "MEMBER_ENROLLMENT.STATUS_CD"

    membership = parse_expression("STATUS_CD IN ('A', 'B')")
    assert isinstance(membership, BinaryOperator)
    assert isinstance(membership.right, ListExpression)
    assert len(membership.right.values) == 2
