"""Shared dialect exceptions and the generator-facing dialect contract."""

from typing import Callable, Protocol

from api.parser.expressions import Expression, FunctionCall, Literal, PortReference


class UnsupportedDialectType(ValueError):
    """Raised when a dialect cannot safely represent a source datatype."""


class UnsupportedDialectFunction(ValueError):
    """Raised when a dialect cannot safely represent a source function."""


class SqlDialect(Protocol):
    name: str
    project_name: str
    profile_name: str
    profile_type: str

    def render_literal(self, literal: Literal) -> str: ...
    def render_unary(self, operator: str, operand: str) -> str: ...
    def render_binary(self, operator: str, left: str, right: str) -> str: ...
    def render_concat(self, arguments: list[str]) -> str: ...
    def render_function(self, function: FunctionCall, arguments: list[str]) -> str: ...
    def render_predicate(
        self,
        expression: Expression,
        resolve_column: Callable[[PortReference], str],
    ) -> str: ...
    def sql_identifier(self, identifier: str, *, context: str = "column") -> str: ...
    def data_type(self, datatype: str, precision: str = "", scale: str = "") -> str: ...
