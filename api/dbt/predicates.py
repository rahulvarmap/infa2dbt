"""Strict parser/validator for the predicate subset accepted in source filters."""

from api.parser.expressions import (
    BinaryOperator,
    Expression,
    ExpressionSyntaxError,
    ListExpression,
    Literal,
    PortReference,
    UnaryOperator,
    parse_expression,
)


_COMPARISONS = {"=", "!=", "<>", "<", "<=", ">", ">="}
_PREDICATE_OPERATORS = _COMPARISONS | {
    "IS NULL",
    "IS NOT NULL",
    "IN",
    "NOT IN",
    "LIKE",
    "NOT LIKE",
}


def parse_simple_predicate(expression: str) -> Expression:
    """Parse a source-filter predicate without accepting functions or arithmetic."""
    parsed = parse_expression(expression)
    _validate_predicate(parsed)
    return parsed


def _validate_scalar(expression: Expression) -> None:
    if isinstance(expression, (Literal, PortReference)):
        return
    raise ExpressionSyntaxError(
        "Source filters support only literals and column references as values."
    )


def _validate_predicate(expression: Expression) -> None:
    if isinstance(expression, UnaryOperator) and expression.operator == "NOT":
        _validate_predicate(expression.operand)
        return
    if not isinstance(expression, BinaryOperator):
        raise ExpressionSyntaxError(
            "A source filter must be a comparison or a boolean predicate."
        )
    if expression.operator in {"AND", "OR"}:
        _validate_predicate(expression.left)
        _validate_predicate(expression.right)
        return
    if expression.operator not in _PREDICATE_OPERATORS:
        raise ExpressionSyntaxError(
            f"Operator '{expression.operator}' is not supported in source filters."
        )
    _validate_scalar(expression.left)
    if expression.operator in {"IS NULL", "IS NOT NULL"}:
        if not isinstance(expression.right, Literal) or expression.right.kind != "null":
            raise ExpressionSyntaxError("IS NULL predicates require a NULL value.")
    elif expression.operator in {"IN", "NOT IN"}:
        if not isinstance(expression.right, ListExpression):
            raise ExpressionSyntaxError("IN predicates require a parenthesized value list.")
        for value in expression.right.values:
            _validate_scalar(value)
    else:
        _validate_scalar(expression.right)
