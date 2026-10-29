"""Deterministic, partial Snowflake dbt project generation from PowerCenter IR."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
import zipfile
from collections import defaultdict
from dataclasses import dataclass, field
from io import BytesIO
from typing import Callable

from api.parser.diagnostics import mapping_identity, validate_parse_result
from api.parser.expressions import (
    BinaryOperator,
    Expression,
    ExpressionSyntaxError,
    FunctionCall,
    Literal,
    PortReference,
    UnaryOperator,
    VariablePortReference,
    parse_expression,
)
from api.parser.identifiers import normalize_identifier, normalize_type_name
from api.parser.models import (
    Diagnostic,
    Instance,
    Mapping,
    ParseResult,
    Port,
    Source,
    Target,
    Transformation,
)
from api.parser.order import topological_order

from .dialects.snowflake import (
    UnsupportedSnowflakeFunction,
    UnsupportedSnowflakeType,
    quote_identifier,
    render_binary,
    render_function,
    render_literal,
    render_unary,
    snowflake_type,
)


DBT_CORE_VERSION = "1.12.6"
DBT_SNOWFLAKE_VERSION = "1.12.3"
PROJECT_NAME = "infa2dbt_snowflake"
SUPPORTED_TRANSFORMATIONS = {"source qualifier", "expression", "filter"}


@dataclass(frozen=True)
class DbtProject:
    files: dict[str, str]
    diagnostics: list[dict]
    blocked_models: list[dict]


@dataclass(frozen=True)
class _SourceInfo:
    identity: str
    source: Source
    group_name: str
    stage_name: str


@dataclass(frozen=True)
class _Relation:
    sql: str
    columns: dict[str, str]
    is_cte: bool = False


@dataclass(frozen=True)
class _Block:
    code: str
    message: str
    instance: str | None = None
    field: str | None = None


class _GenerationBlocked(Exception):
    def __init__(self, block: _Block):
        super().__init__(block.message)
        self.block = block


def snake_case(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    normalized = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", normalized)
    slug = re.sub(r"[^A-Za-z0-9]+", "_", normalized).strip("_").lower()
    slug = slug or "unnamed"
    if slug[0].isdigit():
        slug = f"n_{slug}"
    return slug


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]


def _unique_names(items: list[tuple[str, str]]) -> dict[str, str]:
    grouped: dict[str, list[str]] = defaultdict(list)
    for identity, base in items:
        grouped[base].append(identity)
    result = {}
    used = set()
    for base in sorted(grouped):
        for identity in sorted(grouped[base]):
            candidate = base if len(grouped[base]) == 1 else f"{base}_{_digest(identity)}"
            if candidate in used:
                candidate = f"{candidate}_{_digest(identity + base)}"
            used.add(candidate)
            result[identity] = candidate
    return result


def _source_identity(source: Source) -> str:
    return "/".join(
        normalize_identifier(part) for part in
        (source.repository, source.folder, source.name)
    )


def _definition_name(instance: Instance) -> str:
    return normalize_identifier(instance.transformation_name or instance.name)


def _is_source(instance: Instance) -> bool:
    return (
        normalize_type_name(instance.type) in {"source", "source definition"}
        or normalize_type_name(instance.transformation_type) == "source definition"
    )


def _is_target(instance: Instance) -> bool:
    return (
        normalize_type_name(instance.type) in {"target", "target definition"}
        or normalize_type_name(instance.transformation_type) == "target definition"
    )


def _attribute_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def _source_qualifier_blocks(transformation: Transformation) -> list[_Block]:
    aliases = {
        "sql query": ("SOURCE_QUALIFIER_SQL_OVERRIDE", "SQL override"),
        "sql override": ("SOURCE_QUALIFIER_SQL_OVERRIDE", "SQL override"),
        "sql query override": ("SOURCE_QUALIFIER_SQL_OVERRIDE", "SQL override"),
        "source filter": ("SOURCE_QUALIFIER_SOURCE_FILTER", "source filter"),
        "source filter condition": ("SOURCE_QUALIFIER_SOURCE_FILTER", "source filter"),
        "user defined join": ("SOURCE_QUALIFIER_USER_DEFINED_JOIN", "user-defined join"),
    }
    blocks = []
    for name, value in sorted(transformation.attributes.items()):
        canonical = aliases.get(_attribute_key(name))
        if canonical and value.strip():
            code, label = canonical
            blocks.append(_Block(
                code,
                f"Source Qualifier '{transformation.name}' uses a {label}, which is outside the supported subset.",
                instance=transformation.name,
            ))
    return blocks


def _format_diagnostic(diagnostic: Diagnostic) -> dict:
    return diagnostic.model_dump()


def _render_expression(
    expression: Expression,
    resolve_port: Callable[[PortReference], str],
) -> str:
    if isinstance(expression, Literal):
        return render_literal(expression)
    if isinstance(expression, VariablePortReference):
        return resolve_port(expression)
    if isinstance(expression, PortReference):
        return resolve_port(expression)
    if isinstance(expression, UnaryOperator):
        return render_unary(
            expression.operator,
            _render_expression(expression.operand, resolve_port),
        )
    if isinstance(expression, BinaryOperator):
        return render_binary(
            expression.operator,
            _render_expression(expression.left, resolve_port),
            _render_expression(expression.right, resolve_port),
        )
    if isinstance(expression, FunctionCall):
        arguments = [
            _render_expression(argument, resolve_port)
            for argument in expression.arguments
        ]
        return render_function(expression, arguments)
    raise ValueError(f"Unsupported expression AST node: {type(expression).__name__}.")


class _ProjectBuilder:
    def __init__(self, result: ParseResult):
        self.result = result
        self.files: dict[str, str] = {}
        self.diagnostics: list[dict] = [
            _format_diagnostic(item) for item in validate_parse_result(result)
        ]
        self.blocked_models: list[dict] = []
        self.mapping_ids = {
            id(mapping): mapping_identity(mapping, result.mappings)
            for mapping in result.mappings
        }
        self.mapping_diagnostics: dict[str, list[dict]] = defaultdict(list)
        for diagnostic in self.diagnostics:
            self.mapping_diagnostics[diagnostic["mapping"]].append(diagnostic)
        self.source_info = self._index_sources()
        self.source_blockers: dict[str, list[str]] = {}

    def _index_sources(self) -> dict[str, _SourceInfo]:
        sources = {}
        for mapping in self.result.mappings:
            for source in mapping.sources:
                sources.setdefault(_source_identity(source), source)
        group_keys = {
            identity: "/".join(
                normalize_identifier(part)
                for part in (source.repository, source.folder)
            )
            for identity, source in sources.items()
        }
        group_names = _unique_names([
            (group_key, f"src_{snake_case(source.repository)}_{snake_case(source.folder)}")
            for identity, source in sources.items()
            for group_key in [group_keys[identity]]
        ])
        stage_candidates = [
            (
                identity,
                f"stg_{snake_case(source.name)}",
            )
            for identity, source in sources.items()
        ]
        stage_names = _unique_names(stage_candidates)
        return {
            identity: _SourceInfo(
                identity=identity,
                source=source,
                group_name=group_names[group_keys[identity]],
                stage_name=stage_names[identity],
            )
            for identity, source in sources.items()
        }

    def _add_diagnostic(
        self,
        code: str,
        message: str,
        *,
        mapping: str = "",
        instance: str | None = None,
        field_name: str | None = None,
        severity: str = "error",
    ) -> dict:
        item = {
            "severity": severity,
            "code": code,
            "message": message,
            "mapping": mapping,
            "instance": instance,
            "field": field_name,
        }
        self.diagnostics.append(item)
        if mapping:
            self.mapping_diagnostics[mapping].append(item)
        return item

    def _blocked_model(
        self,
        *,
        model_name: str,
        path: str,
        kind: str,
        mapping: str,
        target: str,
        blocks: list[_Block],
    ) -> None:
        codes = sorted({block.code for block in blocks})
        self.blocked_models.append({
            "name": model_name,
            "path": path,
            "kind": kind,
            "mapping": mapping,
            "target": target,
            "reasons": codes,
        })
        comments = "\n".join(f"-- {code}" for code in codes)
        self.files[path] = (
            f"-- BLOCKED MODEL: {model_name}\n"
            "-- This placeholder intentionally contains no migrated transformation logic.\n"
            f"{comments}\n"
            "{{ config(materialized='view') }}\n\n"
            'select cast(null as variant) as "__INFA2DBT_BLOCKED_MODEL"\n'
            "where 1 = 0\n"
        )

    def _build_staging_models(self) -> None:
        for identity, info in sorted(self.source_info.items()):
            path = f"models/staging/{info.stage_name}.sql"
            source = info.source
            blocks = []
            projections = []
            for field in sorted(source.fields, key=lambda item: normalize_identifier(item.name)):
                try:
                    data_type = snowflake_type(field.datatype, field.precision, field.scale)
                except UnsupportedSnowflakeType as error:
                    block = _Block(
                        "UNSUPPORTED_SOURCE_DATATYPE",
                        str(error),
                        instance=source.name,
                        field=field.name,
                    )
                    blocks.append(block)
                    self._add_diagnostic(
                        block.code, block.message, instance=source.name,
                        field_name=field.name,
                    )
                    continue
                projections.append(
                    f"  cast(src.{quote_identifier(field.name)} as {data_type}) "
                    f"as {quote_identifier(snake_case(field.name))}"
                )
            if not source.fields:
                blocks.append(_Block(
                    "EMPTY_SOURCE_DEFINITION",
                    f"Source '{source.name}' declares no fields.",
                    instance=source.name,
                ))
                self._add_diagnostic(
                    "EMPTY_SOURCE_DEFINITION",
                    f"Source '{source.name}' declares no fields.",
                    instance=source.name,
                )
            if blocks:
                self.source_blockers[identity] = [block.code for block in blocks]
                self._blocked_model(
                    model_name=info.stage_name,
                    path=path,
                    kind="staging",
                    mapping="",
                    target=source.name,
                    blocks=blocks,
                )
            else:
                columns = ",\n".join(projections)
                self.files[path] = (
                    f"{{{{ config(materialized='view') }}}}\n\n"
                    "select\n"
                    f"{columns}\n"
                    f"from {{{{ source('{info.group_name}', '{source.name}') }}}} as src\n"
                )

    def _source_yml(self) -> str:
        groups: dict[str, list[_SourceInfo]] = defaultdict(list)
        for info in self.source_info.values():
            groups[info.group_name].append(info)
        lines = ["version: 2", "", "sources:"]
        for group, infos in sorted(groups.items()):
            sample = infos[0].source
            lines.extend([
                f"  - name: {json.dumps(group)}",
                f"    description: {json.dumps(f'PowerCenter sources from {sample.repository}/{sample.folder}')}",
                '    database: "{{ var(\'source_database\', \'RAW\') }}"',
                '    schema: "{{ var(\'source_schema\', \'PUBLIC\') }}"',
                "    quoting:",
                "      identifier: true",
                "    tables:",
            ])
            for info in sorted(infos, key=lambda item: (item.source.name.lower(), item.identity)):
                lines.extend([
                    f"      - name: {json.dumps(info.source.name)}",
                    f"        description: {json.dumps(f'PowerCenter source {info.source.name}')}",
                ])
        return "\n".join(lines) + "\n"

    def _schema_yml(self, target_entries: list[dict]) -> str:
        lines = ["version: 2", "", "models:"]
        stage_entries = [
            (info.stage_name, info.source.name, info.source.fields, "staging")
            for info in self.source_info.values()
        ]
        all_entries = stage_entries + [
            (entry["model"], entry["target_name"], entry["fields"], "target")
            for entry in target_entries
        ]
        for name, origin, fields, kind in sorted(all_entries, key=lambda item: item[0]):
            lines.extend([
                f"  - name: {json.dumps(name)}",
                f"    description: {json.dumps(f'{kind.title()} model generated from PowerCenter object {origin}.')}",
            ])
            if fields:
                lines.append("    columns:")
                for item in sorted(fields, key=lambda field: normalize_identifier(field.name)):
                    try:
                        mapped_type = snowflake_type(item.datatype, item.precision, item.scale)
                    except UnsupportedSnowflakeType:
                        mapped_type = "UNSUPPORTED"
                    column_name = snake_case(item.name)
                    lines.extend([
                        f"      - name: {json.dumps(column_name)}",
                        f"        description: {json.dumps(f'PowerCenter field {item.name}; Snowflake type {mapped_type}.')}",
                    ])
        return "\n".join(lines) + "\n"

    def _dbt_files(self) -> None:
        self.files["dbt_project.yml"] = (
            f"name: {PROJECT_NAME}\n"
            "version: '1.0.0'\n"
            "config-version: 2\n"
            "profile: infa2dbt\n"
            "model-paths: ['models']\n"
            "analysis-paths: ['analyses']\n"
            "test-paths: ['tests']\n"
            "seed-paths: ['seeds']\n"
            "macro-paths: ['macros']\n"
            "snapshot-paths: ['snapshots']\n"
            "target-path: 'target'\n"
            "clean-targets: ['target', 'dbt_packages']\n"
            "vars:\n"
            "  source_database: RAW\n"
            "  source_schema: PUBLIC\n"
            "models:\n"
            f"  {PROJECT_NAME}:\n"
            "    +materialized: view\n"
        )
        self.files["profiles.yml"] = (
            "infa2dbt:\n"
            "  target: dev\n"
            "  outputs:\n"
            "    dev:\n"
            "      type: snowflake\n"
            "      account: <your-account>\n"
            "      user: <your-user>\n"
            "      password: <your-password>\n"
            "      role: <your-role>\n"
            "      database: <your-database>\n"
            "      warehouse: <your-warehouse>\n"
            "      schema: <your-schema>\n"
            "      threads: 1\n"
        )
        self.files["requirements.txt"] = (
            f"dbt-core=={DBT_CORE_VERSION}\n"
            f"dbt-snowflake=={DBT_SNOWFLAKE_VERSION}\n"
        )
        self.files["README.md"] = (
            "# Generated Snowflake dbt project\n\n"
            "Generated models use views by default. Review `diagnostics.json` before running "
            "the project; blocked models are intentionally non-functional placeholders.\n\n"
            "Copy `profiles.yml` to your local dbt profiles directory and replace every "
            "placeholder with your own Snowflake connection values. Do not commit credentials.\n\n"
            "Install the pinned adapter with `python -m pip install -r requirements.txt`, "
            "then run `dbt parse` from this project directory.\n"
        )

    def _mapping_relations(
        self,
        mapping: Mapping,
        mapping_key: str,
        target_instance: Instance,
        target_model: str,
        target_path: str,
    ) -> str:
        instances = {normalize_identifier(item.name): item for item in mapping.instances}
        parents: dict[str, list] = defaultdict(list)
        incoming: dict[tuple[str, str], list] = defaultdict(list)
        for connector in mapping.connectors:
            from_key = normalize_identifier(connector.from_instance)
            to_key = normalize_identifier(connector.to_instance)
            parents[to_key].append(from_key)
            incoming[(to_key, normalize_identifier(connector.to_field))].append(connector)

        target_key = normalize_identifier(target_instance.name)
        ancestors = {target_key}
        pending = [target_key]
        while pending:
            current = pending.pop()
            for parent in parents.get(current, []):
                if parent not in ancestors:
                    ancestors.add(parent)
                    pending.append(parent)

        sources = {normalize_identifier(item.name): item for item in mapping.sources}
        targets = {normalize_identifier(item.name): item for item in mapping.targets}
        transformations = {
            normalize_identifier(item.name): item for item in mapping.transformations
        }
        target_definition = targets.get(_definition_name(target_instance))
        if target_definition is None:
            raise _GenerationBlocked(_Block(
                "UNRESOLVED_TARGET_DEFINITION",
                f"Target instance '{target_instance.name}' has no target definition.",
                target_instance.name,
            ))
        if not target_definition.fields:
            raise _GenerationBlocked(_Block(
                "EMPTY_TARGET_DEFINITION",
                f"Target '{target_definition.name}' declares no fields.",
                target_instance.name,
            ))

        if len(set(parents.get(target_key, []))) > 1:
            raise _GenerationBlocked(_Block(
                "MULTIPLE_TARGET_INPUTS",
                f"Target '{target_definition.name}' receives multiple input relations; joins are not in the supported subset.",
                target_instance.name,
            ))

        mapping_errors = [
            item for item in self.mapping_diagnostics.get(mapping_key, [])
            if item["severity"] == "error"
        ]
        if mapping_errors:
            raise _GenerationBlocked(_Block(
                "INVALID_MAPPING",
                f"Mapping '{mapping_key}' has validation errors; its target model is blocked.",
                target_instance.name,
            ))

        try:
            ordered_names = topological_order(mapping)
        except ValueError as error:
            raise _GenerationBlocked(_Block(
                "INVALID_MAPPING_GRAPH", str(error), target_instance.name
            )) from error

        source_relations: dict[str, _Relation] = {}
        for instance_key, instance in instances.items():
            if instance_key not in ancestors or not _is_source(instance):
                continue
            definition = sources.get(_definition_name(instance))
            if definition is None:
                continue
            identity = _source_identity(definition)
            info = self.source_info.get(identity)
            if info is None:
                continue
            if identity in self.source_blockers:
                raise _GenerationBlocked(_Block(
                    "BLOCKED_BY_SOURCE_MODEL",
                    f"Target '{target_definition.name}' depends on blocked staging model '{info.stage_name}'.",
                    target_instance.name,
                ))
            source_relations[instance_key] = _Relation(
                sql=f"{{{{ ref('{info.stage_name}') }}}}",
                columns={
                    normalize_identifier(item.name): snake_case(item.name)
                    for item in definition.fields
                },
            )

        relations = dict(source_relations)
        ctes = []
        for instance_name in ordered_names:
            instance_key = normalize_identifier(instance_name)
            if instance_key not in ancestors:
                continue
            instance = instances[instance_key]
            if _is_source(instance) or _is_target(instance):
                continue

            transformation = transformations.get(_definition_name(instance))
            type_name = normalize_type_name(
                transformation.type if transformation else
                instance.transformation_type or instance.type
            )
            if type_name not in SUPPORTED_TRANSFORMATIONS:
                raise _GenerationBlocked(_Block(
                    "UNSUPPORTED_TRANSFORMATION",
                    f"Transformation '{instance.name}' of type '{type_name}' is outside the supported dbt subset.",
                    instance.name,
                ))
            if transformation is None:
                raise _GenerationBlocked(_Block(
                    "UNRESOLVED_TRANSFORMATION_DEFINITION",
                    f"Transformation instance '{instance.name}' has no definition.",
                    instance.name,
                ))

            if type_name == "source qualifier":
                blocks = _source_qualifier_blocks(transformation)
                if blocks:
                    raise _GenerationBlocked(blocks[0])

            parent_keys = sorted(set(parents.get(instance_key, [])))
            if len(parent_keys) > 1:
                raise _GenerationBlocked(_Block(
                    "MULTIPLE_INPUT_RELATIONS",
                    f"Transformation '{instance.name}' has multiple input relations; joins are not in the supported subset.",
                    instance.name,
                ))
            if type_name != "expression" and not parent_keys:
                raise _GenerationBlocked(_Block(
                    "MISSING_TRANSFORMATION_INPUT",
                    f"Transformation '{instance.name}' requires one input relation.",
                    instance.name,
                ))
            parent_relation = relations.get(parent_keys[0]) if parent_keys else None
            if parent_keys and parent_relation is None:
                raise _GenerationBlocked(_Block(
                    "UNRESOLVED_TRANSFORMATION_INPUT",
                    f"Transformation '{instance.name}' depends on an unresolved input relation.",
                    instance.name,
                ))

            ports = {normalize_identifier(port.name): port for port in transformation.ports}
            variable_ports = {
                normalize_identifier(port.name)
                for port in transformation.ports
                if "VARIABLE" in port.porttype.upper()
            }
            relation_alias = "__in"
            active_ports: set[str] = set()

            def resolve_port(reference: PortReference) -> str:
                ref_key = normalize_identifier(reference.name)
                candidate = ports.get(ref_key)
                if isinstance(reference, VariablePortReference) or (
                    candidate is not None
                    and "VARIABLE" in candidate.porttype.upper()
                ):
                    if candidate is None or not candidate.expression:
                        raise _GenerationBlocked(_Block(
                            "UNRESOLVED_VARIABLE_PORT",
                            f"Variable port '{reference.name}' has no expression.",
                            instance.name,
                            reference.name,
                        ))
                    if ref_key in active_ports:
                        raise _GenerationBlocked(_Block(
                            "VARIABLE_PORT_CYCLE",
                            f"Expression contains a local-variable cycle at '{reference.name}'.",
                            instance.name,
                            reference.name,
                        ))
                    active_ports.add(ref_key)
                    try:
                        nested = parse_expression(
                            candidate.expression,
                            variable_ports=variable_ports,
                        )
                        return _render_expression(nested, resolve_port)
                    except ExpressionSyntaxError as error:
                        raise _GenerationBlocked(_Block(
                            "INVALID_EXPRESSION",
                            str(error),
                            instance.name,
                            candidate.name,
                        )) from error
                    except UnsupportedSnowflakeFunction as error:
                        raise _GenerationBlocked(_Block(
                            "UNSUPPORTED_EXPRESSION_FUNCTION",
                            str(error),
                            instance.name,
                            candidate.name,
                        )) from error
                    finally:
                        active_ports.remove(ref_key)

                candidates = incoming.get((instance_key, ref_key), [])
                if len(candidates) > 1:
                    raise _GenerationBlocked(_Block(
                        "AMBIGUOUS_EXPRESSION_PORT",
                        f"Port '{reference.name}' has multiple incoming connectors.",
                        instance.name,
                        reference.name,
                    ))
                if candidates:
                    connector = candidates[0]
                    upstream_key = normalize_identifier(connector.from_instance)
                    upstream = relations.get(upstream_key)
                    if upstream is None:
                        raise _GenerationBlocked(_Block(
                            "UNRESOLVED_EXPRESSION_INPUT",
                            f"Expression port '{reference.name}' references an unresolved input.",
                            instance.name,
                            reference.name,
                        ))
                    output_name = upstream.columns.get(
                        normalize_identifier(connector.from_field)
                    )
                    if output_name is None:
                        raise _GenerationBlocked(_Block(
                            "UNRESOLVED_EXPRESSION_FIELD",
                            f"Input field '{connector.from_field}' is not produced upstream.",
                            instance.name,
                            reference.name,
                        ))
                    return f"{quote_identifier(relation_alias)}.{quote_identifier(output_name)}"

                if (
                    candidate is not None
                    and "OUTPUT" in candidate.porttype.upper()
                    and candidate.expression
                ):
                    if ref_key in active_ports:
                        raise _GenerationBlocked(_Block(
                            "EXPRESSION_PORT_CYCLE",
                            f"Expression contains a local-port cycle at '{reference.name}'.",
                            instance.name,
                            reference.name,
                        ))
                    active_ports.add(ref_key)
                    try:
                        nested = parse_expression(
                            candidate.expression,
                            variable_ports=variable_ports,
                        )
                        return _render_expression(nested, resolve_port)
                    except ExpressionSyntaxError as error:
                        raise _GenerationBlocked(_Block(
                            "INVALID_EXPRESSION",
                            str(error),
                            instance.name,
                            candidate.name,
                        )) from error
                    except UnsupportedSnowflakeFunction as error:
                        raise _GenerationBlocked(_Block(
                            "UNSUPPORTED_EXPRESSION_FUNCTION",
                            str(error),
                            instance.name,
                            candidate.name,
                        )) from error
                    finally:
                        active_ports.remove(ref_key)

                if parent_relation is not None:
                    output_name = parent_relation.columns.get(ref_key)
                    if output_name is not None:
                        return f"{quote_identifier(relation_alias)}.{quote_identifier(output_name)}"
                raise _GenerationBlocked(_Block(
                    "UNRESOLVED_EXPRESSION_PORT",
                    f"Expression references undeclared or unconnected port '{reference.name}'.",
                    instance.name,
                    reference.name,
                ))

            output_ports = [
                port for port in transformation.ports
                if "OUTPUT" in port.porttype.upper()
                and "VARIABLE" not in port.porttype.upper()
            ]
            if not output_ports:
                raise _GenerationBlocked(_Block(
                    "NO_TRANSFORMATION_OUTPUTS",
                    f"Transformation '{instance.name}' has no output ports.",
                    instance.name,
                ))

            projections = []
            columns = {}
            for port in sorted(output_ports, key=lambda item: normalize_identifier(item.name)):
                if port.expression:
                    try:
                        ast = parse_expression(port.expression, variable_ports=variable_ports)
                        expression_sql = _render_expression(ast, resolve_port)
                    except ExpressionSyntaxError as error:
                        raise _GenerationBlocked(_Block(
                            "INVALID_EXPRESSION", str(error), instance.name, port.name
                        )) from error
                    except UnsupportedSnowflakeFunction as error:
                        raise _GenerationBlocked(_Block(
                            "UNSUPPORTED_EXPRESSION_FUNCTION",
                            str(error),
                            instance.name,
                            port.name,
                        )) from error
                else:
                    expression_sql = resolve_port(PortReference(port.name))
                output_name = snake_case(port.name)
                columns[normalize_identifier(port.name)] = output_name
                projections.append(
                    f"    {expression_sql} as {quote_identifier(output_name)}"
                )

            where_sql = ""
            if type_name == "filter":
                condition = next(
                    (
                        value for key, value in transformation.attributes.items()
                        if _attribute_key(key) in {"filter condition", "filter"}
                    ),
                    "",
                )
                if not condition.strip():
                    raise _GenerationBlocked(_Block(
                        "MISSING_FILTER_CONDITION",
                        f"Filter transformation '{instance.name}' has no filter condition.",
                        instance.name,
                    ))
                try:
                    condition_ast = parse_expression(condition, variable_ports=variable_ports)
                    where_sql = _render_expression(condition_ast, resolve_port)
                except ExpressionSyntaxError as error:
                    raise _GenerationBlocked(_Block(
                        "INVALID_FILTER_EXPRESSION",
                        str(error),
                        instance.name,
                    )) from error
                except UnsupportedSnowflakeFunction as error:
                    raise _GenerationBlocked(_Block(
                        "UNSUPPORTED_FILTER_FUNCTION",
                        str(error),
                        instance.name,
                    )) from error

            cte_name = snake_case(instance.name)
            if parent_relation is None:
                body = ",\n".join(projections)
                cte_sql = f"{quote_identifier(cte_name)} as (\n  select\n{body}\n)"
            else:
                from_sql = parent_relation.sql
                if parent_relation.is_cte:
                    from_sql = quote_identifier(from_sql)
                body = ",\n".join(projections)
                where_clause = f"\n  where {where_sql}" if where_sql else ""
                cte_sql = (
                    f"{quote_identifier(cte_name)} as (\n"
                    f"  select\n{body}\n"
                    f"  from {from_sql} as {quote_identifier(relation_alias)}"
                    f"{where_clause}\n)"
                )
            ctes.append(cte_sql)
            relations[instance_key] = _Relation(
                sql=cte_name,
                columns=columns,
                is_cte=True,
            )

        target_fields = []
        target_projections = []
        for field_item in sorted(
            target_definition.fields, key=lambda item: normalize_identifier(item.name)
        ):
            try:
                snowflake_type(field_item.datatype, field_item.precision, field_item.scale)
            except UnsupportedSnowflakeType as error:
                raise _GenerationBlocked(_Block(
                    "UNSUPPORTED_TARGET_DATATYPE",
                    str(error),
                    target_instance.name,
                    field_item.name,
                )) from error
            connectors = incoming.get((target_key, normalize_identifier(field_item.name)), [])
            if len(connectors) != 1:
                raise _GenerationBlocked(_Block(
                    "UNRESOLVED_TARGET_FIELD",
                    f"Target field '{field_item.name}' must have exactly one incoming connector.",
                    target_instance.name,
                    field_item.name,
                ))
            connector = connectors[0]
            relation = relations.get(normalize_identifier(connector.from_instance))
            if relation is None:
                raise _GenerationBlocked(_Block(
                    "UNRESOLVED_TARGET_INPUT",
                    f"Target field '{field_item.name}' has an unresolved input relation.",
                    target_instance.name,
                    field_item.name,
                ))
            relation_field = relation.columns.get(normalize_identifier(connector.from_field))
            if relation_field is None:
                raise _GenerationBlocked(_Block(
                    "UNRESOLVED_TARGET_INPUT_FIELD",
                    f"Upstream field '{connector.from_field}' is not produced for target field '{field_item.name}'.",
                    target_instance.name,
                    field_item.name,
                ))
            target_alias = snake_case(field_item.name)
            target_fields.append(field_item)
            target_projections.append(
                f"  {quote_identifier('target_input')}.{quote_identifier(relation_field)} "
                f"as {quote_identifier(target_alias)}"
            )
        if not target_projections:
            raise _GenerationBlocked(_Block(
                "EMPTY_TARGET_MODEL",
                f"Target '{target_definition.name}' has no connected fields.",
                target_instance.name,
            ))

        target_input = relations[
            normalize_identifier(
                next(
                    connector.from_instance
                    for connector in mapping.connectors
                    if normalize_identifier(connector.to_instance) == target_key
                )
            )
        ]
        from_sql = target_input.sql
        if target_input.is_cte:
            from_sql = quote_identifier(from_sql)
        cte_prefix = "with\n" + ",\n".join(ctes) + "\n\n" if ctes else ""
        target_select = ",\n".join(target_projections)
        return (
            f"{{{{ config(materialized='view') }}}}\n\n"
            f"{cte_prefix}select\n"
            f"{target_select}\n"
            f"from {from_sql} as {quote_identifier('target_input')}\n"
        )

    def _build_targets(self) -> list[dict]:
        candidates = []
        for mapping in self.result.mappings:
            mapping_key = self.mapping_ids[id(mapping)]
            target_defs = {
                normalize_identifier(item.name): item for item in mapping.targets
            }
            for instance in mapping.instances:
                if not _is_target(instance):
                    continue
                definition = target_defs.get(_definition_name(instance))
                name = definition.name if definition else instance.name
                identity = f"{mapping_key}/{instance.name}"
                candidates.append((identity, mapping, instance, definition, name))
        model_names = _unique_names([
            (identity, snake_case(target_name))
            for identity, _, _, _, target_name in candidates
        ])
        schema_entries = []
        for identity, mapping, instance, definition, target_name in sorted(
            candidates, key=lambda item: item[0]
        ):
            mapping_key = self.mapping_ids[id(mapping)]
            model_name = model_names[identity]
            path = f"models/marts/{model_name}.sql"
            fields = definition.fields if definition else []
            blocks = []
            if definition is None:
                blocks.append(_Block(
                    "UNRESOLVED_TARGET_DEFINITION",
                    f"Target instance '{instance.name}' has no target definition.",
                    instance.name,
                ))
            else:
                try:
                    sql = self._mapping_relations(
                        mapping, mapping_key, instance, model_name, path
                    )
                except _GenerationBlocked as error:
                    blocks.append(error.block)
                else:
                    self.files[path] = sql

            for block in blocks:
                self._add_diagnostic(
                    block.code,
                    block.message,
                    mapping=mapping_key,
                    instance=block.instance or instance.name,
                    field_name=block.field,
                )
            if blocks:
                self._blocked_model(
                    model_name=model_name,
                    path=path,
                    kind="target",
                    mapping=mapping_key,
                    target=target_name,
                    blocks=blocks,
                )
            schema_entries.append({
                "model": model_name,
                "target_name": target_name,
                "fields": fields,
            })
        return schema_entries

    def build(self) -> DbtProject:
        self._build_staging_models()
        self.files["models/sources.yml"] = self._source_yml()
        target_entries = self._build_targets()
        self.files["models/schema.yml"] = self._schema_yml(target_entries)
        self._dbt_files()
        self.diagnostics = sorted(
            {
                (
                    item["severity"], item["code"], item["message"], item["mapping"],
                    item["instance"] or "", item["field"] or "",
                ): item
                for item in self.diagnostics
            }.values(),
            key=lambda item: (
                item["mapping"], item["code"], item["instance"] or "",
                item["field"] or "", item["message"],
            ),
        )
        self.blocked_models.sort(key=lambda item: (item["path"], item["mapping"]))
        self.files["diagnostics.json"] = json.dumps({
            "format_version": 1,
            "warehouse": "snowflake",
            "blocked_models": self.blocked_models,
            "diagnostics": self.diagnostics,
        }, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        return DbtProject(
            files=dict(sorted(self.files.items())),
            diagnostics=self.diagnostics,
            blocked_models=self.blocked_models,
        )


def build_project(result: ParseResult) -> DbtProject:
    """Build the deterministic dbt files for a parsed PowerCenter export."""
    return _ProjectBuilder(result).build()


def zip_project(files: dict[str, str]) -> bytes:
    """Create a reproducible ZIP with fixed metadata and path ordering."""
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path, content in sorted(files.items()):
            info = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, content.encode("utf-8"))
    return buffer.getvalue()
