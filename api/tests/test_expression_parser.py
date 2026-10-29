from decimal import Decimal

import pytest

from api.parser.expressions import (
    BinaryOperator,
    ExpressionSyntaxError,
    FunctionCall,
    Literal,
    PortReference,
    UnaryOperator,
    VariablePortReference,
    iter_function_calls,
    iter_port_references,
    parse_expression,
    unknown_function_calls,
)


@pytest.mark.parametrize(("source", "expected"), [
    ("'hello'", ("string", "hello")),
    ("'O''Brien'", ("string", "O'Brien")),
    ("''", ("string", "")),
    ("0", ("number", Decimal("0"))),
    ("17", ("number", Decimal("17"))),
    ("-2.5", None),
    (".25", ("number", Decimal(".25"))),
    ("1.2e3", ("number", Decimal("1.2e3"))),
    ("TRUE", ("boolean", True)),
    ("false", ("boolean", False)),
    ("NULL", ("null", None)),
])
def test_literals(source, expected):
    result = parse_expression(source)
    if source == "-2.5":
        assert isinstance(result, UnaryOperator)
        assert result.operator == "-"
        assert result.operand == Literal("number", Decimal("2.5"))
    else:
        assert isinstance(result, Literal)
        assert (result.kind, result.value) == expected


@pytest.mark.parametrize(("source", "operator"), [
    ("A + B", "+"),
    ("A - B", "-"),
    ("A * B", "*"),
    ("A / B", "/"),
    ("A || B", "||"),
    ("A = B", "="),
    ("A <> B", "<>"),
    ("A != B", "!="),
    ("A < B", "<"),
    ("A <= B", "<="),
    ("A > B", ">"),
    ("A >= B", ">="),
    ("A AND B", "AND"),
    ("A OR B", "OR"),
    ("A IS NULL", "IS NULL"),
    ("A IS NOT NULL", "IS NOT NULL"),
])
def test_binary_operators(source, operator):
    result = parse_expression(source)
    assert isinstance(result, BinaryOperator)
    assert result.operator == operator


@pytest.mark.parametrize(("source", "function_name"), [
    ("IIF(A = 1, B, C)", "IIF"),
    ("DECODE(A, 'x', B, C)", "DECODE"),
    ("ISNULL(A)", "ISNULL"),
    ("NVL(A, 0)", "NVL"),
    ("LTRIM(A)", "LTRIM"),
    ("RTRIM(A)", "RTRIM"),
    ("SUBSTR(A, 1, 2)", "SUBSTR"),
    ("CONCAT(A, B)", "CONCAT"),
    ("TO_DATE(A, 'YYYY-MM-DD')", "TO_DATE"),
    ("TO_CHAR(A, 'YYYY')", "TO_CHAR"),
    ("SYSDATE", "SYSDATE"),
    ("SYSDATE()", "SYSDATE"),
])
def test_supported_functions(source, function_name):
    result = parse_expression(source)
    assert isinstance(result, FunctionCall)
    assert result.name == function_name


@pytest.mark.parametrize("source,expected_root", [
    ("A + B * C", "+"),
    ("A * B + C", "+"),
    ("A = B OR C = D", "OR"),
    ("A = B AND C = D", "AND"),
    ("NOT A = B", "NOT"),
])
def test_operator_precedence(source, expected_root):
    result = parse_expression(source)
    if expected_root == "NOT":
        assert isinstance(result, UnaryOperator)
        assert isinstance(result.operand, BinaryOperator)
    else:
        assert isinstance(result, BinaryOperator)
        assert result.operator == expected_root


def test_references_preserve_expression_order_and_ignore_function_names():
    result = parse_expression("IIF(A = 1, NVL(B, C), DECODE(D, 'x', E))")
    assert [reference.name for reference in iter_port_references(result)] == [
        "A", "B", "C", "D", "E",
    ]
    assert [call.name for call in iter_function_calls(result)] == [
        "IIF", "NVL", "DECODE",
    ]


def test_variable_references_have_a_distinct_ast_node():
    result = parse_expression("v + Input", variable_ports={"V"})
    assert isinstance(result, BinaryOperator)
    assert isinstance(result.left, VariablePortReference)
    assert isinstance(result.right, PortReference)


def test_unknown_function_is_preserved_as_a_function_node():
    result = parse_expression("VENDOR_FUNC(A, 2)")
    assert isinstance(result, FunctionCall)
    assert result.name == "VENDOR_FUNC"
    assert [call.name for call in unknown_function_calls(result)] == ["VENDOR_FUNC"]
    assert [reference.name for reference in iter_port_references(result)] == ["A"]


@pytest.mark.parametrize("source", [
    "",
    "A +",
    "IIF(A, B",
    "A & B",
    "A 2",
    "CONCAT(A,)",
    "'unterminated",
])
def test_invalid_expressions_raise_a_clear_syntax_error(source):
    with pytest.raises(ExpressionSyntaxError):
        parse_expression(source)
