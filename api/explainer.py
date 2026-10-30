"""Deterministic plain-English explanations for parsed PowerCenter mappings."""

from __future__ import annotations

from datetime import date
from typing import Any

from api.dbt.generator import DbtProject, snake_case
from api.parser.expressions import (
    BinaryOperator,
    Expression,
    ExpressionSyntaxError,
    FunctionCall,
    Literal,
    PortReference,
    UnaryOperator,
    iter_port_references,
    parse_expression,
    unknown_function_calls,
)
from api.parser.identifiers import normalize_identifier, normalize_type_name
from api.parser.lineage import mapping_key
from api.parser.models import Instance, Mapping, ParseResult, Transformation
from api.parser.order import topological_order


def _key(value: str) -> str:
    return normalize_identifier(value.split(".")[-1])


def _humanize(value: str) -> str:
    text = value.split(".")[-1]
    parts = [part.lower() for part in text.replace("-", "_").split("_") if part]
    replacements = {
        "dob": "date of birth",
        "dt": "date",
        "cd": "code",
        "id": "ID",
        "amt": "amount",
        "qty": "quantity",
    }
    words = [replacements.get(part, part) for part in parts]
    if not words:
        return value
    result = " ".join(words)
    return result[:1].upper() + result[1:]


def _literal_text(expression: Literal) -> str:
    if expression.kind == "null":
        return "missing"
    if expression.kind == "boolean":
        return "true" if expression.value else "false"
    if expression.kind == "number":
        return str(expression.value)
    value = str(expression.value)
    try:
        return date.fromisoformat(value).strftime("%d-%b-%Y")
    except ValueError:
        return value


def _describe(expression: Expression) -> str:
    if isinstance(expression, Literal):
        return _literal_text(expression)
    if isinstance(expression, PortReference):
        return _humanize(expression.name)
    if isinstance(expression, UnaryOperator):
        operand = _describe(expression.operand)
        if expression.operator.upper() == "NOT":
            return f"not ({operand})"
        return f"{expression.operator}{operand}"
    if isinstance(expression, BinaryOperator):
        if expression.operator == "||":
            terms = []

            def collect(term: Expression) -> None:
                if isinstance(term, BinaryOperator) and term.operator == "||":
                    collect(term.left)
                    collect(term.right)
                else:
                    terms.append(term)

            collect(expression)
            descriptions = []
            for term in terms:
                if (
                    isinstance(term, Literal)
                    and term.kind == "string"
                    and not str(term.value).strip()
                ):
                    descriptions.append("a space")
                else:
                    descriptions.append(_describe(term))
            return "Joined from " + ", ".join(descriptions)
        left = _describe(expression.left)
        right = _describe(expression.right)
        if expression.operator.upper() in {"AND", "OR"}:
            return f"{left} {expression.operator.lower()} {right}"
        words = {
            "=": "equals",
            "!=": "does not equal",
            "<>": "does not equal",
            ">": "is greater than",
            ">=": "is at least",
            "<": "is less than",
            "<=": "is at most",
            "IS NULL": "is missing",
            "IS NOT NULL": "is present",
            "LIKE": "matches",
            "NOT LIKE": "does not match",
            "IN": "is one of",
            "NOT IN": "is not one of",
            "+": "plus",
            "-": "minus",
            "*": "times",
            "/": "divided by",
        }
        return f"{left} {words.get(expression.operator, expression.operator)} {right}"
    if not isinstance(expression, FunctionCall):
        return "the value"

    name = normalize_identifier(expression.name)
    args = list(expression.arguments)
    described = [_describe(argument) for argument in args]
    if name == "sysdate":
        return "the current date and time"
    if name == "isnull" and args:
        return f"{described[0]} is missing"
    if name == "nvl" and len(args) == 2:
        return f"{described[0]}, or {described[1]} if missing"
    if name == "iif" and len(args) == 3:
        condition = args[0]
        if (
            isinstance(condition, FunctionCall)
            and normalize_identifier(condition.name) == "isnull"
            and condition.arguments
        ):
            subject = _describe(condition.arguments[0])
            fallback = _describe(args[1])
            present = _describe(args[2])
            missing_text = (
                "missing" if subject.lower() == present.lower()
                else f"{subject.lower()} is missing"
            )
            return f"{present}, or {fallback} if {missing_text}"
        return (
            f"{described[1]} if {described[0]}; "
            f"otherwise {described[2]}"
        )
    if name == "decode" and len(args) >= 3:
        clauses = []
        has_default = len(args) % 2 == 0
        pair_end = len(args) - 1 if has_default else len(args)
        for index in range(1, pair_end, 2):
            clauses.append(
                f"{_literal_text(args[index])} → {described[index + 1]}"
            )
        result = f"Map {described[0]}: {'; '.join(clauses)}"
        if has_default:
            result += f"; otherwise {described[-1]}"
        return result
    if name in {"sum", "count", "avg", "min", "max"} and args:
        labels = {
            "sum": "Sum of",
            "count": "Count of",
            "avg": "Average of",
            "min": "Smallest",
            "max": "Largest",
        }
        if name == "sum" and len(args) == 2:
            return (
                f"Sum of {described[0]} for rows where "
                f"{described[1]}"
            )
        return f"{labels[name]} {described[0]}"
    if name == "trunc" and args:
        if (
            isinstance(args[0], FunctionCall)
            and normalize_identifier(args[0].name) == "date_diff"
            and len(args[0].arguments) >= 2
        ):
            return (
                f"Whole years between {_describe(args[0].arguments[1])} "
                f"and {_describe(args[0].arguments[0])}"
            )
        return f"Whole-number value of {described[0]}"
    if name == "date_diff" and len(args) == 3:
        unit = _literal_text(args[2]).lower()
        unit_name = {"yyyy": "years", "yy": "years", "mm": "months"}.get(
            unit, unit
        )
        return f"{unit_name} between {described[1]} and {described[0]}"
    if name == "concat":
        return " combined with ".join(described)
    if name in {"ltrim", "rtrim"} and args:
        if isinstance(args[0], FunctionCall) and normalize_identifier(args[0].name) in {
            "ltrim", "rtrim",
        }:
            return f"{_describe(args[0].arguments[0])} with leading and trailing spaces removed"
        edge = "leading" if name == "ltrim" else "trailing"
        return f"{described[0]} with {edge} spaces removed"
    if name in {"to_date", "to_char"} and args:
        if name == "to_date":
            return f"{described[0]} converted to a date"
        return f"{described[0]} formatted as text"
    if name == "substr" and args:
        return f"Part of {described[0]}"
    if next(unknown_function_calls(expression), None) is not None:
        return "Unknown function; see the raw expression."
    return f"{expression.name}({', '.join(described)})"


def _attribute(transformation: Transformation, *names: str) -> str:
    wanted = {normalize_identifier(name).replace("_", " ") for name in names}
    for name, value in transformation.attributes.items():
        canonical = normalize_identifier(name).replace("_", " ")
        if canonical in wanted and value.strip():
            return value.strip()
    return ""


def _source_instance(instance: Instance) -> bool:
    return (
        normalize_type_name(instance.type) in {"source", "source definition"}
        or normalize_type_name(instance.transformation_type) == "source definition"
    )


def _target_instance(instance: Instance) -> bool:
    return (
        normalize_type_name(instance.type) in {"target", "target definition"}
        or normalize_type_name(instance.transformation_type) == "target definition"
    )


def _transform_for(
    instance: Instance,
    transformations: dict[str, Transformation],
) -> Transformation | None:
    return transformations.get(
        normalize_identifier(instance.transformation_name or instance.name)
    )


def _source_names_for_instance(mapping: Mapping, instance_name: str) -> list[str]:
    instances = {normalize_identifier(item.name): item for item in mapping.instances}
    names = []
    for connector in mapping.connectors:
        if normalize_identifier(connector.to_instance) != normalize_identifier(instance_name):
            continue
        source_instance = instances.get(normalize_identifier(connector.from_instance))
        if source_instance is None or not _source_instance(source_instance):
            continue
        definition_name = normalize_identifier(
            source_instance.transformation_name or source_instance.name
        )
        source = next(
            (
                item for item in mapping.sources
                if normalize_identifier(item.name) == definition_name
            ),
            None,
        )
        if source is not None:
            names.append(source.name)
    return sorted(set(names), key=normalize_identifier)


def _rule_for_target(
    mapping: Mapping,
    target_instance: str,
    target_field: str,
    transformations: dict[str, Transformation],
) -> tuple[list[str], str, str]:
    instances = {normalize_identifier(item.name): item for item in mapping.instances}
    incoming: dict[tuple[str, str], list[Any]] = {}
    for connector in mapping.connectors:
        incoming.setdefault(
            (
                normalize_identifier(connector.to_instance),
                _key(connector.to_field),
            ),
            [],
        ).append(connector)

    def walk(
        instance_name: str,
        field_name: str,
        visited: set[tuple[str, str]],
    ) -> tuple[list[str], str, str]:
        instance_key = normalize_identifier(instance_name)
        field_key = _key(field_name)
        visit_key = (instance_key, field_key)
        if visit_key in visited:
            return [], "", ""
        visited = {*visited, visit_key}
        instance = instances.get(instance_key)
        if instance is None:
            return [], "", ""

        if _source_instance(instance):
            definition_name = normalize_identifier(
                instance.transformation_name or instance.name
            )
            source = next(
                (
                    item for item in mapping.sources
                    if normalize_identifier(item.name) == definition_name
                ),
                None,
            )
            if source is not None:
                return [f"{source.name}.{field_name.split('.')[-1]}"], "", ""

        transformation = _transform_for(instance, transformations)
        if transformation is not None:
            port = next(
                (
                    item for item in transformation.ports
                    if normalize_identifier(item.name) == field_key
                ),
                None,
            )
            if port is not None and port.expression.strip():
                raw = port.expression.strip()
                try:
                    ast = parse_expression(
                        raw,
                        variable_ports=[
                            item.name for item in transformation.ports
                            if "VARIABLE" in item.porttype.upper()
                        ],
                    )
                    sources = []
                    for reference in iter_port_references(ast):
                        reference_key = _key(reference.name)
                        if (
                            reference_key == field_key
                            and incoming.get((instance_key, reference_key))
                        ):
                            referenced = []
                            for connector in incoming[(instance_key, reference_key)]:
                                from_sources, _, _ = walk(
                                    connector.from_instance,
                                    connector.from_field,
                                    visited,
                                )
                                referenced.extend(from_sources)
                        else:
                            referenced, _, _ = walk(
                                instance.name,
                                reference.name,
                                visited,
                            )
                        for source_name in referenced:
                            if source_name not in sources:
                                sources.append(source_name)
                    return sources, raw, _describe_ast_or_unknown(ast, raw)
                except ExpressionSyntaxError:
                    return [], raw, "Raw expression; it could not be interpreted safely."

        sources: list[str] = []
        rules: list[tuple[str, str]] = []
        for connector in incoming.get((instance_key, field_key), []):
            found_sources, expression, rule = walk(
                connector.from_instance,
                connector.from_field,
                visited,
            )
            for source_name in found_sources:
                if source_name not in sources:
                    sources.append(source_name)
            if expression or rule:
                candidate = (expression, rule)
                if candidate not in rules:
                    rules.append(candidate)
        if rules:
            return sources, rules[0][0], rules[0][1]
        return sources, "", ""

    return walk(target_instance, target_field, set())


def _describe_ast_or_unknown(expression: Expression, raw: str) -> str:
    unknown = list(unknown_function_calls(expression))
    if unknown:
        names = ", ".join(dict.fromkeys(call.name for call in unknown))
        return (
            f"Raw expression; function {names} is not recognized by the "
            "plain-English explainer."
        )
    return _describe(expression)


def _condition_description(raw: str) -> str:
    if not raw:
        return ""
    try:
        ast = parse_expression(raw)
        return _describe_ast_or_unknown(ast, raw)
    except ExpressionSyntaxError:
        return raw


def _flow_description(
    mapping: Mapping,
    instance: Instance,
    transformation: Transformation,
) -> str:
    type_name = normalize_type_name(transformation.type or instance.transformation_type)
    if type_name == "source qualifier":
        sources = _source_names_for_instance(mapping, instance.name)
        subject = ", ".join(sources) if sources else "the connected source"
        source_filter = _attribute(transformation, "source filter", "source filter condition")
        if source_filter:
            return f"Reads {subject} rows that meet this source filter: {source_filter}."
        return f"Reads and passes through rows from {subject}."

    if type_name == "joiner":
        join_type = _attribute(transformation, "join type") or "Normal"
        condition = _attribute(transformation, "join condition")
        keys = condition
        try:
            ast = parse_expression(condition)
            if (
                isinstance(ast, BinaryOperator)
                and ast.operator == "="
                and isinstance(ast.left, PortReference)
                and isinstance(ast.right, PortReference)
            ):
                keys = (
                    f"{_humanize(ast.left.name)} matches "
                    f"{_humanize(ast.right.name)}"
                )
        except ExpressionSyntaxError:
            pass
        semantics = {
            "normal": "keeps only rows with a match in both master and detail inputs",
            "master outer": "keeps every detail row and matching master rows",
            "detail outer": "keeps every master row and matching detail rows",
            "full outer": "keeps all rows from both inputs, filling unmatched columns with NULL",
        }.get(normalize_identifier(join_type), "uses the configured row-retention behavior")
        return f"{join_type} join on {keys}; it {semantics}."

    if type_name == "lookup":
        table = _attribute(transformation, "lookup table name") or "the lookup table"
        condition = _attribute(transformation, "lookup condition")
        defaults = [
            f"{port.name} defaults to {port.default_value.strip()}"
            for port in transformation.ports
            if port.default_value.strip()
        ]
        description = f"Looks up {table}"
        if condition:
            description += f" where {condition}"
        if defaults:
            description += "; on no match, " + ", ".join(defaults)
        else:
            description += "; unmatched lookups return NULL"
        return description + "."

    if type_name == "filter":
        condition = _attribute(transformation, "filter condition", "filter")
        return (
            f"Keeps rows where {condition}."
            if condition else "Filters rows using the configured condition."
        )

    if type_name == "aggregator":
        group_ports = [
            port.name for port in transformation.ports
            if port.group_by
        ]
        aggregates = []
        for port in transformation.ports:
            if not port.expression.strip() or port.group_by:
                continue
            try:
                ast = parse_expression(port.expression)
                if isinstance(ast, FunctionCall) and normalize_identifier(ast.name) in {
                    "sum", "count", "avg", "min", "max",
                }:
                    aggregates.append(f"{port.name}: {_describe_ast_or_unknown(ast, port.expression)}")
            except ExpressionSyntaxError:
                continue
        group_text = (
            f"Groups by {', '.join(_humanize(name) for name in group_ports)}"
            if group_ports else "Creates one aggregate row"
        )
        if aggregates:
            group_text += "; computes " + "; ".join(aggregates)
        return group_text + "."

    if type_name == "expression":
        derivations = []
        for port in transformation.ports:
            if "OUTPUT" not in port.porttype.upper() or not port.expression.strip():
                continue
            try:
                ast = parse_expression(port.expression)
                rule = _describe_ast_or_unknown(ast, port.expression)
            except ExpressionSyntaxError:
                rule = "Raw expression; it could not be interpreted safely."
            derivations.append(f"{_humanize(port.name)}: {rule}")
        if derivations:
            return "Derives " + "; ".join(derivations) + "."
        return "Passes the connected values through."

    return f"Applies the {transformation.type or 'unknown'} transformation."


def _overview(mapping: Mapping) -> str:
    sources = ", ".join(item.name for item in mapping.sources) or "the connected sources"
    targets = ", ".join(item.name for item in mapping.targets) or "the configured targets"
    transforms = {
        normalize_type_name(item.type): item for item in mapping.transformations
    }
    purpose = ""
    if "aggregator" in transforms:
        group_ports = [
            port.name
            for port in transforms["aggregator"].ports
            if port.group_by
        ]
        if group_ports:
            group_names = ", ".join(_humanize(name).lower() for name in group_ports)
            purpose = f"summarizing the data by {group_names}"
        else:
            purpose = "creating a summary across the input rows"
    elif "joiner" in transforms:
        purpose = "combining related records"
    elif "lookup" in transforms:
        purpose = "enriching records with lookup details"

    filters = [
        _attribute(item, "filter condition", "filter")
        or _attribute(item, "source filter", "source filter condition")
        for item in mapping.transformations
        if normalize_type_name(item.type) in {"filter", "source qualifier"}
    ]
    filters = [item for item in filters if item]
    for condition in filters:
        normalized = normalize_identifier(condition)
        if "status_cd = 'a'" in normalized or "status_cd='a'" in normalized:
            purpose = "keeping active members"
            break
    if not purpose and filters:
        purpose = f"keeping rows that satisfy {_condition_description(filters[0])}"
    if not purpose:
        purpose = "preparing the source data for its target"
    return f"Builds {targets} from {sources}, {purpose}."


def explain_mapping(
    result: ParseResult,
    mapping: Mapping,
    project: DbtProject,
) -> dict[str, Any]:
    identity = mapping_key(mapping, result.mappings)
    transformations = {
        normalize_identifier(item.name): item
        for item in mapping.transformations
    }
    instances = {
        normalize_identifier(item.name): item for item in mapping.instances
    }
    steps = []
    try:
        order = topological_order(mapping)
    except ValueError:
        order = [item.name for item in mapping.instances]
    for instance_name in order:
        instance = instances.get(normalize_identifier(instance_name))
        if instance is None or _source_instance(instance) or _target_instance(instance):
            continue
        transformation = _transform_for(instance, transformations)
        if transformation is None:
            steps.append({
                "instance": instance.name,
                "type": instance.transformation_type or instance.type,
                "description": (
                    "This transformation is present in the mapping but its "
                    "definition was not resolved."
                ),
            })
            continue
        steps.append({
            "instance": instance.name,
            "type": transformation.type,
            "description": _flow_description(mapping, instance, transformation),
        })

    columns = []
    for target in mapping.targets:
        target_instances = [
            item for item in mapping.instances
            if _target_instance(item)
            and normalize_identifier(item.transformation_name or item.name)
            == normalize_identifier(target.name)
        ]
        if not target_instances:
            target_instances = [
                item for item in mapping.instances
                if _target_instance(item)
            ]
        for field in target.fields:
            for target_instance in target_instances:
                source_columns, expression, plain_rule = _rule_for_target(
                    mapping,
                    target_instance.name,
                    field.name,
                    transformations,
                )
                connectors = [
                    item for item in mapping.connectors
                    if normalize_identifier(item.to_instance)
                    == normalize_identifier(target_instance.name)
                    and _key(item.to_field) == normalize_identifier(field.name)
                ]
                if not connectors:
                    continue
                columns.append({
                    "target_column": field.name,
                    "source_columns": source_columns,
                    "expression": expression,
                    "rule": plain_rule or "Passed through unchanged.",
                })
                break

    diagnostics = [
        item for item in project.diagnostics
        if normalize_identifier(str(item.get("mapping", "")))
        == normalize_identifier(identity)
    ]
    blocked = [
        item for item in project.blocked_models
        if normalize_identifier(str(item.get("mapping", "")))
        == normalize_identifier(identity)
    ]
    model_paths: dict[str, dict[str, str]] = {}
    for source in mapping.sources:
        name = f"stg_{snake_case(source.name)}"
        path = f"models/staging/{name}.sql"
        if path in project.files:
            model_paths[path] = {"name": name, "status": "generated"}

    for target in mapping.targets:
        expected_path = f"models/marts/{snake_case(target.name)}.sql"
        blocked_model = next(
            (
                item for item in blocked
                if normalize_identifier(str(item.get("name", "")))
                == normalize_identifier(target.name)
            ),
            None,
        )
        path = (
            str(blocked_model["path"])
            if blocked_model is not None
            else expected_path
        )
        if path in project.files or blocked_model is not None:
            model_paths[path] = {
                "name": str(
                    blocked_model.get("name", snake_case(target.name))
                    if blocked_model is not None else snake_case(target.name)
                ),
                "status": "blocked" if blocked_model is not None else "generated",
            }

    generated_models = [
        {"name": value["name"], "path": path, "status": value["status"]}
        for path, value in sorted(model_paths.items())
    ]
    target_models = [
        item for item in generated_models
        if item["path"].startswith("models/marts/")
    ]
    return {
        "mapping": identity,
        "overview": _overview(mapping),
        "steps": steps,
        "columns": columns,
        "migration_status": {
            "supported": sum(item["status"] == "generated" for item in target_models),
            "warning": sum(item.get("severity") == "warning" for item in diagnostics),
            "blocked": sum(item["status"] == "blocked" for item in target_models),
        },
        "generated_models": generated_models,
        "diagnostics": diagnostics,
    }


def explain_result(result: ParseResult, project: DbtProject) -> dict[str, dict[str, Any]]:
    """Build deterministic explanations keyed by the same mapping identity as the API."""
    return {
        mapping_key(mapping, result.mappings): explain_mapping(result, mapping, project)
        for mapping in result.mappings
    }
