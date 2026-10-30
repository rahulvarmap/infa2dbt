"""Small, non-evaluating parser for Informatica expression syntax."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable, Iterator, Literal as TypingLiteral, TypeAlias

from .identifiers import normalize_identifier


KNOWN_FUNCTIONS = frozenset({
    "iif", "decode", "isnull", "nvl", "ltrim", "rtrim", "substr",
    "concat", "to_date", "to_char", "sysdate", "date_diff", "trunc",
    "sum", "count", "avg", "min", "max",
})


class ExpressionSyntaxError(ValueError):
    """Raised when an expression cannot be parsed without guessing."""


@dataclass(frozen=True)
class Literal:
    kind: TypingLiteral["string", "number", "boolean", "null"]
    value: str | Decimal | bool | None


@dataclass(frozen=True)
class PortReference:
    name: str


@dataclass(frozen=True)
class VariablePortReference(PortReference):
    """A reference to a transformation-local variable port."""


@dataclass(frozen=True)
class FunctionCall:
    name: str
    arguments: tuple[Expression, ...]


@dataclass(frozen=True)
class UnaryOperator:
    operator: str
    operand: Expression


@dataclass(frozen=True)
class BinaryOperator:
    operator: str
    left: Expression
    right: Expression


@dataclass(frozen=True)
class ListExpression:
    values: tuple[Expression, ...]


Expression: TypeAlias = (
    Literal | PortReference | VariablePortReference | FunctionCall
    | UnaryOperator | BinaryOperator | ListExpression
)


@dataclass(frozen=True)
class _Token:
    kind: str
    value: str
    offset: int


_TOKEN_PATTERN = re.compile(
    r"(?P<space>\s+)"
    r"|(?P<string>'(?:''|[^'])*')"
    r"|(?P<number>(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)"
    r"|(?P<identifier>(?:[^\W\d]|_|\$)[\w$]*(?:\.(?:[^\W\d]|_|\$)[\w$]*)*)"
    r"|(?P<operator>\|\||<=|>=|<>|!=|[()+\-*/=<>!,])",
    re.UNICODE,
)

_PRECEDENCE = {
    "OR": 1,
    "AND": 2,
    "=": 3,
    "!=": 3,
    "<>": 3,
    "<": 3,
    "<=": 3,
    ">": 3,
    ">=": 3,
    "IS NULL": 3,
    "IS NOT NULL": 3,
    "IN": 3,
    "NOT IN": 3,
    "LIKE": 3,
    "NOT LIKE": 3,
    "||": 4,
    "+": 5,
    "-": 5,
    "*": 6,
    "/": 6,
}


def _tokenize(expression: str) -> list[_Token]:
    tokens = []
    offset = 0
    while offset < len(expression):
        match = _TOKEN_PATTERN.match(expression, offset)
        if match is None:
            raise ExpressionSyntaxError(
                f"Unexpected character at position {offset}: {expression[offset]!r}."
            )
        kind = match.lastgroup
        value = match.group()
        if kind != "space":
            tokens.append(_Token(kind or "", value, offset))
        offset = match.end()
    tokens.append(_Token("eof", "", len(expression)))
    return tokens


class _Parser:
    def __init__(self, expression: str, variable_ports: Iterable[str]):
        self.expression = expression
        self.tokens = _tokenize(expression)
        self.index = 0
        self.variable_ports = {
            normalize_identifier(name) for name in variable_ports
        }

    @property
    def current(self) -> _Token:
        return self.tokens[self.index]

    def _advance(self) -> _Token:
        token = self.current
        self.index += 1
        return token

    def _expect(self, value: str) -> _Token:
        if self.current.value != value:
            raise ExpressionSyntaxError(
                f"Expected {value!r} at position {self.current.offset}."
            )
        return self._advance()

    def parse(self) -> Expression:
        if self.current.kind == "eof":
            raise ExpressionSyntaxError("Expression is empty.")
        result = self._parse_binary(0)
        if self.current.kind != "eof":
            raise ExpressionSyntaxError(
                f"Unexpected token {self.current.value!r} at position {self.current.offset}."
            )
        return result

    def _parse_binary(self, minimum_precedence: int) -> Expression:
        left = self._parse_prefix()
        while True:
            operator = self._binary_operator()
            if operator is None:
                break
            precedence = _PRECEDENCE[operator]
            if precedence < minimum_precedence:
                break
            if operator in {"IS NULL", "IS NOT NULL"}:
                self._advance()  # IS
                if operator == "IS NOT NULL":
                    self._advance()  # NOT
                self._advance()  # NULL
                right = Literal("null", None)
            elif operator in {"IN", "NOT IN"}:
                self._advance()
                if operator == "NOT IN":
                    self._advance()
                self._expect("(")
                values = []
                if self.current.kind == "operator" and self.current.value == ")":
                    raise ExpressionSyntaxError("IN requires at least one value.")
                while True:
                    values.append(self._parse_binary(0))
                    if self.current.kind == "operator" and self.current.value == ",":
                        self._advance()
                        continue
                    self._expect(")")
                    break
                right = ListExpression(tuple(values))
            else:
                self._advance()
                if operator == "NOT LIKE":
                    self._advance()
                right = self._parse_binary(precedence + 1)
            left = BinaryOperator(operator, left, right)
        return left

    def _binary_operator(self) -> str | None:
        token = self.current
        if token.kind == "operator" and token.value in _PRECEDENCE:
            return token.value
        if token.kind != "identifier":
            return None
        keyword = token.value.upper()
        if keyword in {"AND", "OR"}:
            return keyword
        if keyword in {"IN", "LIKE"}:
            return keyword
        if (
            keyword == "NOT"
            and self.tokens[self.index + 1].kind == "identifier"
            and self.tokens[self.index + 1].value.upper() in {"IN", "LIKE"}
        ):
            return f"NOT {self.tokens[self.index + 1].value.upper()}"
        if keyword == "IS":
            next_token = self.tokens[self.index + 1]
            if next_token.kind == "identifier" and next_token.value.upper() == "NULL":
                return "IS NULL"
            if (
                next_token.kind == "identifier"
                and next_token.value.upper() == "NOT"
                and self.tokens[self.index + 2].kind == "identifier"
                and self.tokens[self.index + 2].value.upper() == "NULL"
            ):
                return "IS NOT NULL"
        return None

    def _parse_prefix(self) -> Expression:
        token = self.current
        if token.kind == "operator" and token.value in {"+", "-"}:
            self._advance()
            return UnaryOperator(token.value, self._parse_binary(7))
        if token.kind == "identifier" and token.value.upper() == "NOT":
            self._advance()
            return UnaryOperator("NOT", self._parse_binary(3))
        if token.kind == "operator" and token.value == "(":
            self._advance()
            expression = self._parse_binary(0)
            self._expect(")")
            return expression
        if token.kind == "string":
            self._advance()
            return Literal("string", token.value[1:-1].replace("''", "'"))
        if token.kind == "number":
            self._advance()
            return Literal("number", Decimal(token.value))
        if token.kind == "identifier":
            return self._parse_identifier()
        raise ExpressionSyntaxError(
            f"Expected a literal, port reference, function, or grouped expression at position {token.offset}."
        )

    def _parse_identifier(self) -> Expression:
        token = self._advance()
        keyword = token.value.upper()
        if keyword == "NULL":
            return Literal("null", None)
        if keyword in {"TRUE", "FALSE"}:
            return Literal("boolean", keyword == "TRUE")

        if self.current.kind == "operator" and self.current.value == "(":
            self._advance()
            arguments = []
            if self.current.kind == "operator" and self.current.value == ")":
                self._advance()
            else:
                while True:
                    arguments.append(self._parse_binary(0))
                    if self.current.kind == "operator" and self.current.value == ",":
                        self._advance()
                        continue
                    self._expect(")")
                    break
            return FunctionCall(token.value, tuple(arguments))

        if keyword == "SYSDATE":
            return FunctionCall(token.value, ())
        if normalize_identifier(token.value) in self.variable_ports:
            return VariablePortReference(token.value)
        return PortReference(token.value)


def parse_expression(
    expression: str,
    *,
    variable_ports: Iterable[str] = (),
) -> Expression:
    """Parse an expression into an AST without executing its functions."""
    return _Parser(expression, variable_ports).parse()


def iter_port_references(expression: Expression) -> Iterator[PortReference]:
    if isinstance(expression, PortReference):
        yield expression
    elif isinstance(expression, UnaryOperator):
        yield from iter_port_references(expression.operand)
    elif isinstance(expression, BinaryOperator):
        yield from iter_port_references(expression.left)
        yield from iter_port_references(expression.right)
    elif isinstance(expression, ListExpression):
        for value in expression.values:
            yield from iter_port_references(value)
    elif isinstance(expression, FunctionCall):
        for argument in expression.arguments:
            yield from iter_port_references(argument)


def iter_function_calls(expression: Expression) -> Iterator[FunctionCall]:
    if isinstance(expression, FunctionCall):
        yield expression
        for argument in expression.arguments:
            yield from iter_function_calls(argument)
    elif isinstance(expression, UnaryOperator):
        yield from iter_function_calls(expression.operand)
    elif isinstance(expression, BinaryOperator):
        yield from iter_function_calls(expression.left)
        yield from iter_function_calls(expression.right)
    elif isinstance(expression, ListExpression):
        for value in expression.values:
            yield from iter_function_calls(value)


def unknown_function_calls(expression: Expression) -> Iterator[FunctionCall]:
    for call in iter_function_calls(expression):
        if normalize_identifier(call.name) not in KNOWN_FUNCTIONS:
            yield call
