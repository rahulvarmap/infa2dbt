"""DuckDB SQL translation used only for local generated-project validation."""

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

from .base import UnsupportedDialectFunction, UnsupportedDialectType


name = "duckdb"
project_name = "infa2dbt_duckdb"
profile_name = "infa2dbt_duckdb"
profile_type = "duckdb"


class UnsupportedDuckDBType(UnsupportedDialectType):
    """Raised when a PowerCenter datatype cannot map safely to DuckDB."""


class UnsupportedDuckDBFunction(UnsupportedDialectFunction):
    """Raised when an expression cannot map safely to DuckDB."""


_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
_RESERVED = frozenset({
    "ALL", "ALTER", "AND", "ANY", "AS", "ASC", "BETWEEN", "BY", "CASE",
    "CAST", "CHECK", "COLUMN", "CONSTRAINT", "CREATE", "CURRENT_DATE",
    "CURRENT_TIME", "CURRENT_TIMESTAMP", "CURRENT_USER", "DELETE", "DESC",
    "DISTINCT", "DROP", "ELSE", "END", "EXCEPT", "EXISTS", "FALSE", "FETCH",
    "FILTER", "FOR", "FOREIGN", "FROM", "FULL", "GRANT", "GROUP", "HAVING",
    "ILIKE", "IN", "INNER", "INSERT", "INTERSECT", "INTO", "IS", "JOIN",
    "LATERAL", "LEFT", "LIKE", "LIMIT", "NATURAL", "NOT", "NULL", "OFFSET",
    "ON", "OR", "ORDER", "OUTER", "OVER", "PARTITION", "QUALIFY", "RANGE",
    "RECURSIVE", "RIGHT", "ROW", "ROWS", "SELECT", "SET", "TABLE", "THEN",
    "TO", "TRUE", "UNION", "UNIQUE", "UNPIVOT", "UPDATE", "USING", "VALUES",
    "WHEN", "WHERE", "WINDOW", "WITH",
})


def quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def sql_identifier(identifier: str, *, context: str = "column") -> str:
    if context not in {"column", "relation"}:
        raise ValueError(f"Unsupported SQL identifier context: {context}.")
    if _IDENTIFIER.fullmatch(identifier) and identifier.upper() not in _RESERVED:
        return identifier
    return quote_identifier(identifier)


def lookup_relation(
    source_sql: str,
    columns: list[str],
    partition_columns: list[str],
    *,
    last_value: bool,
) -> str:
    """Use a ranked subquery to select one stable row per lookup key."""
    input_alias = "lookup_candidate"
    ranked_alias = "lookup_ranked"
    output_alias = "lkp"
    projections = ", ".join(
        f"{sql_identifier(ranked_alias, context='relation')}."
        f"{sql_identifier(column)}"
        for column in columns
    )
    input_projections = ", ".join(
        f"{sql_identifier(input_alias, context='relation')}."
        f"{sql_identifier(column)}"
        for column in columns
    )
    partitions = ", ".join(
        f"{sql_identifier(input_alias, context='relation')}."
        f"{sql_identifier(column)}"
        for column in partition_columns
    )
    direction = "DESC NULLS LAST" if last_value else "ASC NULLS FIRST"
    ordering = ", ".join(
        f"{sql_identifier(input_alias, context='relation')}."
        f"{sql_identifier(column)} {direction}"
        for column in columns
    )
    return (
        "(\n"
        f"  select {projections}\n"
        "  from (\n"
        f"    select {input_projections},\n"
        "      ROW_NUMBER() OVER (\n"
        f"        PARTITION BY {partitions}\n"
        f"        ORDER BY {ordering}\n"
        "      ) as lookup_row_number\n"
        f"    from {source_sql} as "
        f"{sql_identifier(input_alias, context='relation')}\n"
        f"  ) as {sql_identifier(ranked_alias, context='relation')}\n"
        f"  where {sql_identifier(ranked_alias, context='relation')}."
        f"{sql_identifier('lookup_row_number')} = 1\n"
        f") as {sql_identifier(output_alias, context='relation')}"
    )


def render_literal(literal: Literal) -> str:
    if literal.kind == "null":
        return "NULL"
    if literal.kind == "boolean":
        return "TRUE" if literal.value else "FALSE"
    if literal.kind == "number":
        return format(literal.value, "f") if isinstance(literal.value, Decimal) else str(literal.value)
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


def render_concat(arguments: list[str]) -> str:
    if len(arguments) < 2:
        raise UnsupportedDuckDBFunction("CONCAT requires at least two arguments.")
    all_null = " AND ".join(f"({argument}) IS NULL" for argument in arguments)
    joined = " || ".join(
        f"COALESCE(({argument}), '')" for argument in arguments
    )
    return f"(CASE WHEN {all_null} THEN NULL ELSE ({joined}) END)"


def _sql_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


_FORMAT_TOKENS = {
    "YYYY": "%Y",
    "YYY": "%Y",
    "YY": "%y",
    "MONTH": "%B",
    "MON": "%b",
    "MM": "%m",
    "DD": "%d",
    "DAY": "%A",
    "DY": "%a",
    "HH24": "%H",
    "HH12": "%I",
    "HH": "%I",
    "MI": "%M",
    "SS": "%S",
}


def _duckdb_format(source_format: str) -> str:
    token_pattern = re.compile(
        "|".join(
            re.escape(token)
            for token in sorted(_FORMAT_TOKENS, key=len, reverse=True)
        ),
        re.IGNORECASE,
    )
    return token_pattern.sub(
        lambda match: _FORMAT_TOKENS[match.group(0).upper()],
        source_format,
    )


def _months_between(first: str, second: str) -> str:
    left = f"CAST({first} AS TIMESTAMP)"
    right = f"CAST({second} AS TIMESTAMP)"
    whole_months = f"date_diff('month', {right}, {left})"
    same_day = f"extract(day FROM {left}) = extract(day FROM {right})"
    left_month_end = (
        f"CAST({left} AS DATE) = CAST(date_trunc('month', {left}) "
        "+ interval '1 month' - interval '1 day' AS DATE)"
    )
    right_month_end = (
        f"CAST({right} AS DATE) = CAST(date_trunc('month', {right}) "
        "+ interval '1 month' - interval '1 day' AS DATE)"
    )
    day_fraction = (
        f"(extract(day FROM {left}) - extract(day FROM {right})) / 31.0"
    )
    return (
        f"(CASE WHEN ({same_day}) OR (({left_month_end}) AND ({right_month_end})) "
        f"THEN {whole_months} ELSE ({whole_months} + {day_fraction}) END)"
    )


_DATE_DIFF_UNITS = {
    "Y": "year", "YY": "year", "YYY": "year", "YYYY": "year", "RR": "year",
    "MM": "month", "MON": "month", "MONTH": "month",
    "D": "day", "DD": "day", "DDD": "day", "DAY": "day", "DY": "day", "J": "day",
    "HH": "hour", "HH12": "hour", "HH24": "hour", "MI": "minute", "SS": "second",
    "SSSS": "second", "MS": "millisecond",
}
_TRUNC_DATE_PARTS = {
    "Y": "year", "YY": "year", "YYY": "year", "YYYY": "year",
    "MM": "month", "MON": "month", "MONTH": "month",
    "D": "day", "DD": "day", "DDD": "day", "DAY": "day", "DY": "day",
    "HH": "hour", "HH12": "hour", "HH24": "hour", "MI": "minute", "SS": "second",
}


def render_function(function: FunctionCall, arguments: list[str]) -> str:
    name = function.name.upper()
    count = len(arguments)
    if name == "SYSDATE":
        _require_arity(name, count, {0})
        return "CURRENT_TIMESTAMP::TIMESTAMP"
    if name == "IIF":
        _require_arity(name, count, {3})
        return f"(CASE WHEN {arguments[0]} THEN {arguments[1]} ELSE {arguments[2]} END)"
    if name == "DECODE":
        if count < 3:
            raise UnsupportedDuckDBFunction("DECODE requires at least three arguments.")
        has_default = count % 2 == 0
        pair_end = count - 1 if has_default else count
        clauses = [
            f"WHEN ({arguments[0]} IS NOT DISTINCT FROM {arguments[index]}) "
            f"THEN {arguments[index + 1]}"
            for index in range(1, pair_end, 2)
        ]
        otherwise = arguments[-1] if has_default else "NULL"
        return f"(CASE {' '.join(clauses)} ELSE {otherwise} END)"
    if name == "ISNULL":
        _require_arity(name, count, {1})
        return f"({arguments[0]} IS NULL)"
    if name == "NVL":
        _require_arity(name, count, {2})
        return f"COALESCE({', '.join(arguments)})"
    if name in {"LTRIM", "RTRIM", "SUBSTR"}:
        accepted = {"LTRIM": {1, 2}, "RTRIM": {1, 2}, "SUBSTR": {2, 3}}[name]
        _require_arity(name, count, accepted)
        sql_name = "substring" if name == "SUBSTR" else name.lower()
        return f"{sql_name}({', '.join(arguments)})"
    if name == "CONCAT":
        return render_concat(arguments)
    if name == "TO_DATE":
        _require_arity(name, count, {1, 2})
        if count == 1:
            return f"CAST({arguments[0]} AS DATE)"
        format_argument = function.arguments[1]
        if not isinstance(format_argument, Literal) or format_argument.kind != "string":
            raise UnsupportedDuckDBFunction("TO_DATE requires a string-literal format.")
        fmt = _sql_string(_duckdb_format(str(format_argument.value)))
        return f"CAST(strptime({arguments[0]}, {fmt}) AS DATE)"
    if name == "TO_CHAR":
        _require_arity(name, count, {1, 2})
        if count == 1:
            return f"CAST({arguments[0]} AS VARCHAR)"
        format_argument = function.arguments[1]
        if not isinstance(format_argument, Literal) or format_argument.kind != "string":
            raise UnsupportedDuckDBFunction("TO_CHAR requires a string-literal format.")
        fmt = _sql_string(_duckdb_format(str(format_argument.value)))
        return f"strftime({arguments[0]}, {fmt})"
    if name == "DATE_DIFF":
        _require_arity(name, count, {3})
        unit_argument = function.arguments[2]
        if not isinstance(unit_argument, Literal) or unit_argument.kind != "string":
            raise UnsupportedDuckDBFunction(
                "DATE_DIFF requires a supported string-literal date unit."
            )
        unit_token = str(unit_argument.value).upper()
        if unit_token in {"YYYY", "MM"}:
            month_delta = _months_between(arguments[0], arguments[1])
            return f"({month_delta} / 12)" if unit_token == "YYYY" else month_delta
        unit = _DATE_DIFF_UNITS.get(unit_token)
        if unit is None:
            raise UnsupportedDuckDBFunction(
                f"DATE_DIFF unit '{unit_argument.value}' has no DuckDB translation."
            )
        return f"date_diff('{unit}', {arguments[1]}, {arguments[0]})"
    if name == "TRUNC":
        if count == 1:
            argument = function.arguments[0]
            if isinstance(argument, FunctionCall) and argument.name.upper() == "SYSDATE":
                return f"date_trunc('day', {arguments[0]})"
            return f"trunc({arguments[0]})"
        if count == 2:
            second = function.arguments[1]
            if isinstance(second, Literal) and second.kind == "string":
                date_part = _TRUNC_DATE_PARTS.get(str(second.value).upper())
                if date_part is None:
                    raise UnsupportedDuckDBFunction(
                        f"TRUNC date format '{second.value}' has no DuckDB translation."
                    )
                return f"date_trunc('{date_part}', {arguments[0]})"
            if isinstance(second, Literal) and second.kind == "number":
                return f"trunc({arguments[0]}, {arguments[1]})"
        raise UnsupportedDuckDBFunction(
            "TRUNC requires one expression or a supported second argument."
        )
    raise UnsupportedDuckDBFunction(
        f"Function '{function.name}' has no DuckDB translation."
    )


def render_predicate(
    expression: Expression,
    resolve_column: Callable[[PortReference], str],
) -> str:
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
        return "(" + ", ".join(
            render_predicate(item, resolve_column) for item in expression.values
        ) + ")"
    raise ValueError(
        f"Unsupported predicate AST node: {type(expression).__name__}."
    )


def data_type(datatype: str, precision: str = "", scale: str = "") -> str:
    name = " ".join(datatype.lower().replace("_", " ").replace("-", " ").split())
    try:
        length = int(precision) if precision else 0
        decimal_scale = int(scale) if scale else 0
    except ValueError as error:
        raise UnsupportedDuckDBType(
            f"Invalid precision or scale for Informatica type '{datatype}'."
        ) from error
    if length < 0 or decimal_scale < 0:
        raise UnsupportedDuckDBType(
            f"Invalid precision or scale for Informatica type '{datatype}'."
        )
    if name in {"varchar", "varchar2", "nvarchar", "nvarchar2", "string", "char", "nchar", "text", "ntext", "long varchar"}:
        return "VARCHAR"
    if name in {"number", "numeric", "decimal", "dec"}:
        if length:
            if length > 38 or decimal_scale > length:
                raise UnsupportedDuckDBType(
                    f"Precision/scale ({length}, {decimal_scale}) exceeds DuckDB DECIMAL limits."
                )
            return f"DECIMAL({length}, {decimal_scale})"
        if decimal_scale:
            raise UnsupportedDuckDBType(
                f"Scale {decimal_scale} requires a precision for Informatica type '{datatype}'."
            )
        return "DECIMAL(38, 9)"
    if name in {"integer", "int", "small integer", "smallint"}:
        return "SMALLINT" if name in {"small integer", "smallint"} else "INTEGER"
    if name in {"bigint", "long"}:
        return "BIGINT"
    if name in {"float", "double", "double precision", "real"}:
        return "DOUBLE"
    if name in {"date", "date time", "datetime", "timestamp", "timestamp ntz"}:
        return "TIMESTAMP"
    if name == "time":
        return "TIME"
    if name in {"binary", "varbinary"}:
        return "BLOB"
    if name in {"boolean", "bool"}:
        return "BOOLEAN"
    raise UnsupportedDuckDBType(
        f"Informatica datatype '{datatype}' has no DuckDB mapping."
    )


def _require_arity(name: str, count: int, accepted: set[int]) -> None:
    if count not in accepted:
        expected = " or ".join(str(value) for value in sorted(accepted))
        raise UnsupportedDuckDBFunction(
            f"{name} requires {expected} argument(s), received {count}."
        )
