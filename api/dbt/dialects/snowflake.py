"""Snowflake-specific expression and datatype translation."""

from decimal import Decimal
import re
from typing import Callable

from api.parser.expressions import (
    BinaryOperator,
    Expression,
    FunctionCall,
    ListExpression,
    Literal,
    PortReference,
    UnaryOperator,
)
from api.parser.identifiers import normalize_identifier
from .base import UnsupportedDialectFunction, UnsupportedDialectType


name = "snowflake"
project_name = "infa2dbt_snowflake"
profile_name = "infa2dbt"
profile_type = "snowflake"


class UnsupportedSnowflakeType(UnsupportedDialectType):
    """Raised when an Informatica datatype has no safe Snowflake mapping."""


class UnsupportedSnowflakeFunction(UnsupportedDialectFunction):
    """Raised when a function has no supported Snowflake translation."""


_UNQUOTED_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
_SNOWFLAKE_RESERVED_WORDS = frozenset({
    "ALL", "ALTER", "AND", "ANY", "ASC", "AS", "BETWEEN", "BY", "CHECK",
    "COLUMN", "CONNECT", "CREATE", "CURRENT", "CURRENT_DATE", "CURRENT_TIME",
    "CURRENT_TIMESTAMP", "CURRENT_USER", "DELETE", "DESC", "DISTINCT", "DROP",
    "ELSE", "EXISTS", "FOLLOWING", "FOR", "FROM", "GRANT", "GROUP",
    "HAVING", "ILIKE", "IN", "INCREMENT", "INSERT", "INTERSECT", "INTO", "IS",
    "LIKE", "LOCALTIME", "LOCALTIMESTAMP", "MINUS", "NOT", "NULL", "OF", "ON",
    "OR", "ORDER", "QUALIFY", "REGEXP", "REVOKE", "RLIKE", "ROW", "ROWS",
    "SAMPLE", "SELECT", "SET", "SOME", "START", "TABLE", "TABLESAMPLE", "THEN",
    "TO", "TRIGGER", "UNION", "UNIQUE", "UPDATE", "VALUES", "WHENEVER", "WHERE",
    "WINDOW", "WITH",
})
_COLUMN_RESTRICTED_KEYWORDS = frozenset({
    "CASE", "CAST", "FALSE", "TRUE", "TRY_CAST", "WHEN",
})
_RELATION_RESTRICTED_KEYWORDS = frozenset({
    "CROSS", "FULL", "INNER", "JOIN", "LATERAL", "LEFT", "NATURAL", "RIGHT",
    "USING",
})


def quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def sql_identifier(identifier: str, *, context: str = "column") -> str:
    """Keep ordinary identifiers unquoted; quote only reserved or invalid names."""
    if context == "column":
        restricted = _COLUMN_RESTRICTED_KEYWORDS
    elif context == "relation":
        restricted = _RELATION_RESTRICTED_KEYWORDS
    else:
        raise ValueError(f"Unsupported SQL identifier context: {context}.")
    if (
        _UNQUOTED_IDENTIFIER.fullmatch(identifier)
        and identifier.upper() not in (_SNOWFLAKE_RESERVED_WORDS | restricted)
    ):
        return identifier
    return quote_identifier(identifier)


def render_literal(literal: Literal) -> str:
    if literal.kind == "null":
        return "NULL"
    if literal.kind == "boolean":
        return "TRUE" if literal.value else "FALSE"
    if literal.kind == "number":
        value = literal.value
        if isinstance(value, Decimal):
            return format(value, "f")
        return str(value)
    if literal.kind == "string":
        return "'" + str(literal.value).replace("'", "''") + "'"
    raise ValueError(f"Unsupported literal kind: {literal.kind}.")


def render_unary(operator: str, operand: str) -> str:
    if operator == "NOT":
        return f"(NOT {operand})"
    if operator in {"+", "-"}:
        return f"({operator}{operand})"
    raise ValueError(f"Unsupported unary operator: {operator}.")


def render_binary(operator: str, left: str, right: str) -> str:
    if operator not in {
        "+", "-", "*", "/", "||", "=", "!=", "<>", "<", "<=", ">", ">=",
        "AND", "OR", "IS NULL", "IS NOT NULL", "IN", "NOT IN", "LIKE",
        "NOT LIKE",
    }:
        raise ValueError(f"Unsupported binary operator: {operator}.")
    if operator in {"IS NULL", "IS NOT NULL"}:
        return f"({left} {operator})"
    if operator == "||":
        return render_concat([left, right])
    return f"({left} {operator} {right})"


def render_function(function: FunctionCall, arguments: list[str]) -> str:
    name = function.name.upper()
    count = len(arguments)
    if name == "SYSDATE":
        _require_arity(name, count, {0})
        return "CURRENT_TIMESTAMP()::TIMESTAMP_NTZ"
    if name == "IIF":
        _require_arity(name, count, {3})
        return f"IFF({', '.join(arguments)})"
    if name == "DECODE":
        if count < 3:
            raise UnsupportedSnowflakeFunction("DECODE requires at least three arguments.")
        return f"DECODE({', '.join(arguments)})"
    if name == "ISNULL":
        _require_arity(name, count, {1})
        return f"({arguments[0]} IS NULL)"
    if name in {"NVL", "LTRIM", "RTRIM", "SUBSTR", "TO_DATE"}:
        allowed = {
            "NVL": {2},
            "LTRIM": {1, 2},
            "RTRIM": {1, 2},
            "SUBSTR": {2, 3},
            "TO_DATE": {1, 2},
        }[name]
        _require_arity(name, count, allowed)
        return f"{name}({', '.join(arguments)})"
    if name == "CONCAT":
        if count < 2:
            raise UnsupportedSnowflakeFunction("CONCAT requires at least two arguments.")
        return render_concat(arguments)
    if name == "TO_CHAR":
        _require_arity(name, count, {1, 2})
        return f"TO_VARCHAR({', '.join(arguments)})"
    if name == "DATE_DIFF":
        _require_arity(name, count, {3})
        unit_argument = function.arguments[2]
        if not isinstance(unit_argument, Literal) or unit_argument.kind != "string":
            raise UnsupportedSnowflakeFunction(
                "DATE_DIFF requires a supported string-literal date unit."
            )
        unit_token = str(unit_argument.value).upper()
        if unit_token == "YYYY":
            return (
                f"(MONTHS_BETWEEN({arguments[0]}, {arguments[1]}) / 12)"
            )
        if unit_token == "MM":
            return f"MONTHS_BETWEEN({arguments[0]}, {arguments[1]})"
        unit = _DATE_DIFF_UNITS.get(unit_token)
        if unit is None:
            raise UnsupportedSnowflakeFunction(
                f"DATE_DIFF unit '{unit_argument.value}' has no Snowflake translation."
            )
        # PowerCenter DATE_DIFF returns date1 minus date2; Snowflake DATEDIFF
        # returns expr2 minus expr1, so reverse the date arguments.
        return f"DATEDIFF({unit}, {arguments[1]}, {arguments[0]})"
    if name == "TRUNC":
        if count == 1:
            argument = function.arguments[0]
            if isinstance(argument, FunctionCall):
                argument_name = normalize_identifier(argument.name)
                if argument_name == "sysdate":
                    return f"DATE_TRUNC('DAY', {arguments[0]})"
                if argument_name == "date_diff":
                    return f"TRUNC({arguments[0]})"
            raise UnsupportedSnowflakeFunction(
                "TRUNC requires a recognized date or numeric expression."
            )
        if count == 2:
            second = function.arguments[1]
            if isinstance(second, Literal) and second.kind == "string":
                date_part = _TRUNC_DATE_PARTS.get(str(second.value).upper())
                if date_part is None:
                    raise UnsupportedSnowflakeFunction(
                        f"TRUNC date format '{second.value}' has no Snowflake translation."
                    )
                return f"DATE_TRUNC('{date_part}', {arguments[0]})"
            if isinstance(second, Literal) and second.kind == "number":
                return f"TRUNC({arguments[0]}, {arguments[1]})"
        raise UnsupportedSnowflakeFunction(
            "TRUNC requires one recognized expression or a supported second argument."
        )
    raise UnsupportedSnowflakeFunction(
        f"Function '{function.name}' has no Snowflake translation."
    )


def _render_null_ignoring_concat(arguments: list[str]) -> str:
    all_null = " AND ".join(f"({argument}) IS NULL" for argument in arguments)
    joined = " || ".join(
        f"COALESCE(({argument}), '')" for argument in arguments
    )
    return f"(CASE WHEN {all_null} THEN NULL ELSE ({joined}) END)"


def render_concat(arguments: list[str]) -> str:
    """Render PowerCenter concatenation with NULL operands ignored."""
    if len(arguments) < 2:
        raise ValueError("Concatenation requires at least two operands.")
    return _render_null_ignoring_concat(arguments)


_DATE_DIFF_UNITS = {
    "Y": "year",
    "YY": "year",
    "YYY": "year",
    "YYYY": "year",
    "RR": "year",
    "MM": "month",
    "MON": "month",
    "MONTH": "month",
    "D": "day",
    "DD": "day",
    "DDD": "day",
    "DAY": "day",
    "DY": "day",
    "J": "day",
    "HH": "hour",
    "HH12": "hour",
    "HH24": "hour",
    "MI": "minute",
    "SS": "second",
    "SSSS": "second",
    "MS": "millisecond",
}

_TRUNC_DATE_PARTS = {
    "Y": "YEAR",
    "YY": "YEAR",
    "YYY": "YEAR",
    "YYYY": "YEAR",
    "MM": "MONTH",
    "MON": "MONTH",
    "MONTH": "MONTH",
    "D": "DAY",
    "DD": "DAY",
    "DDD": "DAY",
    "DAY": "DAY",
    "DY": "DAY",
    "HH": "HOUR",
    "HH12": "HOUR",
    "HH24": "HOUR",
    "MI": "MINUTE",
    "SS": "SECOND",
}


def render_predicate(
    expression: Expression,
    resolve_column: Callable[[PortReference], str],
) -> str:
    """Render a validated predicate AST using Snowflake SQL syntax."""
    if isinstance(expression, Literal):
        return render_literal(expression)
    if isinstance(expression, PortReference):
        return resolve_column(expression)
    if isinstance(expression, UnaryOperator):
        return render_unary(
            expression.operator,
            render_predicate(expression.operand, resolve_column),
        )
    if isinstance(expression, BinaryOperator):
        return render_binary(
            expression.operator,
            render_predicate(expression.left, resolve_column),
            render_predicate(expression.right, resolve_column),
        )
    if isinstance(expression, ListExpression):
        values = ", ".join(
            render_predicate(item, resolve_column) for item in expression.values
        )
        return f"({values})"
    raise ValueError(
        f"Unsupported predicate AST node: {type(expression).__name__}."
    )


def _require_arity(name: str, count: int, accepted: set[int]) -> None:
    if count not in accepted:
        expected = " or ".join(str(value) for value in sorted(accepted))
        raise UnsupportedSnowflakeFunction(
            f"{name} requires {expected} argument(s), received {count}."
        )


def snowflake_type(datatype: str, precision: str = "", scale: str = "") -> str:
    """Map a PowerCenter datatype to a Snowflake type or fail explicitly."""
    name = " ".join(datatype.lower().replace("_", " ").replace("-", " ").split())
    try:
        length = int(precision) if precision else 0
        decimal_scale = int(scale) if scale else 0
    except ValueError as error:
        raise UnsupportedSnowflakeType(
            f"Invalid precision or scale for Informatica type '{datatype}'."
        ) from error
    if length < 0 or decimal_scale < 0:
        raise UnsupportedSnowflakeType(
            f"Invalid precision or scale for Informatica type '{datatype}'."
        )

    if name in {"varchar", "varchar2", "nvarchar", "nvarchar2", "string", "char", "nchar"}:
        if length > 16_777_216:
            raise UnsupportedSnowflakeType(
                f"Precision {length} exceeds Snowflake's maximum VARCHAR size."
            )
        return f"VARCHAR({length})" if length else "VARCHAR"
    if name in {"text", "ntext", "long varchar"}:
        return "VARCHAR"
    if name in {"number", "numeric", "decimal", "dec"}:
        if length:
            if length > 38 or decimal_scale > min(length, 37):
                raise UnsupportedSnowflakeType(
                    f"Precision/scale ({length}, {decimal_scale}) exceeds Snowflake NUMBER limits."
                )
            return f"NUMBER({length}, {decimal_scale})"
        if decimal_scale:
            raise UnsupportedSnowflakeType(
                f"Scale {decimal_scale} requires a precision for Informatica type '{datatype}'."
            )
        return "NUMBER"
    if name in {"integer", "int", "small integer", "smallint"}:
        return "NUMBER(5, 0)" if name in {"small integer", "smallint"} else "NUMBER(10, 0)"
    if name in {"bigint", "long"}:
        return "NUMBER(19, 0)"
    if name in {"float", "double", "double precision", "real"}:
        return "FLOAT"
    if name in {"date", "date time", "datetime", "timestamp", "timestamp ntz"}:
        return "TIMESTAMP_NTZ"
    if name in {"time"}:
        return "TIME"
    if name in {"binary", "varbinary"}:
        return "BINARY"
    if name in {"boolean", "bool"}:
        return "BOOLEAN"
    raise UnsupportedSnowflakeType(
        f"Informatica datatype '{datatype}' has no Snowflake mapping."
    )


data_type = snowflake_type
