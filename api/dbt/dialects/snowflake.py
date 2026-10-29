"""Snowflake-specific expression and datatype translation."""

from decimal import Decimal

from api.parser.expressions import BinaryOperator, FunctionCall, Literal, UnaryOperator


class UnsupportedSnowflakeType(ValueError):
    """Raised when an Informatica datatype has no safe Snowflake mapping."""


class UnsupportedSnowflakeFunction(ValueError):
    """Raised when a function has no supported Snowflake translation."""


def quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


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
        "AND", "OR", "IS NULL", "IS NOT NULL",
    }:
        raise ValueError(f"Unsupported binary operator: {operator}.")
    if operator in {"IS NULL", "IS NOT NULL"}:
        return f"({left} {operator})"
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
        return f"CONCAT({', '.join(arguments)})"
    if name == "TO_CHAR":
        _require_arity(name, count, {1, 2})
        return f"TO_VARCHAR({', '.join(arguments)})"
    raise UnsupportedSnowflakeFunction(
        f"Function '{function.name}' has no Snowflake translation."
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
