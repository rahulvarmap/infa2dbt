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
    ListExpression,
    Literal,
    PortReference,
    UnaryOperator,
    VariablePortReference,
    iter_port_references,
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

from .dependency_lock import DBT_REQUIREMENTS_LOCK
from .dialects import get_dialect
from .dialects.base import SqlDialect, UnsupportedDialectFunction, UnsupportedDialectType
from .predicates import parse_simple_predicate


DBT_CORE_VERSION = "1.12.6"
DBT_SNOWFLAKE_VERSION = "1.12.4"
SUPPORTED_TRANSFORMATIONS = {"source qualifier", "expression", "filter"}


@dataclass(frozen=True)
class DbtProject:
    files: dict[str, str]
    diagnostics: list[dict]
    blocked_models: list[dict]
    seed_tables: list["SeedTable"] = field(default_factory=list)


@dataclass(frozen=True)
class SeedTable:
    filename: str
    identifier: str
    source: Source


@dataclass(frozen=True)
class _SourceInfo:
    identity: str
    source: Source
    group_name: str
    stage_name: str
    seed_identifier: str


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


@dataclass(frozen=True)
class _SourceFilterSpec:
    expression: Expression


class _GenerationBlocked(Exception):
    def __init__(self, blocks: _Block | list[_Block]):
        self.blocks = [blocks] if isinstance(blocks, _Block) else blocks
        super().__init__(self.blocks[0].message)


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


def _jinja_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


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


def _concat_expression_terms(expression: Expression) -> list[Expression]:
    if isinstance(expression, BinaryOperator) and expression.operator == "||":
        return (
            _concat_expression_terms(expression.left)
            + _concat_expression_terms(expression.right)
        )
    return [expression]


def _render_expression(
    expression: Expression,
    resolve_port: Callable[[PortReference], str],
    dialect: SqlDialect,
) -> str:
    if isinstance(expression, Literal):
        return dialect.render_literal(expression)
    if isinstance(expression, VariablePortReference):
        return resolve_port(expression)
    if isinstance(expression, PortReference):
        return resolve_port(expression)
    if isinstance(expression, UnaryOperator):
        return dialect.render_unary(
            expression.operator,
            _render_expression(expression.operand, resolve_port, dialect),
        )
    if isinstance(expression, BinaryOperator):
        if expression.operator == "||":
            return dialect.render_concat([
                _render_expression(term, resolve_port, dialect)
                for term in _concat_expression_terms(expression)
            ])
        return dialect.render_binary(
            expression.operator,
            _render_expression(expression.left, resolve_port, dialect),
            _render_expression(expression.right, resolve_port, dialect),
        )
    if isinstance(expression, ListExpression):
        values = ", ".join(
            _render_expression(item, resolve_port, dialect) for item in expression.values
        )
        return f"({values})"
    if isinstance(expression, FunctionCall):
        arguments = [
            _render_expression(argument, resolve_port, dialect)
            for argument in expression.arguments
        ]
        return dialect.render_function(expression, arguments)
    raise ValueError(f"Unsupported expression AST node: {type(expression).__name__}.")


class _ProjectBuilder:
    def __init__(self, result: ParseResult, dialect: SqlDialect):
        self.result = result
        self.dialect = dialect
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
        self.source_filter_blocks: dict[str, list[_Block]] = defaultdict(list)
        self.source_filters = self._index_source_filters()

    def _record_source_filter_block(
        self,
        mapping_key: str,
        instance_name: str,
        code: str,
        message: str,
    ) -> _Block:
        block = _Block(code, message, instance_name, "Source Filter")
        self.source_filter_blocks[mapping_key].append(block)
        self._add_diagnostic(
            code,
            message,
            mapping=mapping_key,
            instance=instance_name,
            field_name="Source Filter",
        )
        return block

    def _validate_source_filter_column(
        self,
        source: Source,
        reference: PortReference,
    ) -> None:
        parts = reference.name.split(".")
        column_name = parts[-1]
        if (
            len(parts) > 1
            and normalize_identifier(parts[-2]) != normalize_identifier(source.name)
        ):
            raise ValueError(
                f"Source filter column '{reference.name}' is qualified by an "
                f"unknown source; expected '{source.name}'."
            )
        field = next(
            (
                item for item in source.fields
                if normalize_identifier(item.name) == normalize_identifier(column_name)
            ),
            None,
        )
        if field is None:
            raise ValueError(
                f"Source filter references unknown column '{reference.name}' "
                f"on source '{source.name}'."
            )

    def _source_filter_relation_column(
        self,
        relation: _Relation,
        relation_alias: str,
        reference: PortReference,
    ) -> str:
        output_name = relation.columns.get(
            normalize_identifier(reference.name.split(".")[-1])
        )
        if output_name is None:
            raise ValueError(
                f"Source filter references unconnected column '{reference.name}'."
            )
        return (
            f"{self.dialect.sql_identifier(relation_alias, context='relation')}."
            f"{self.dialect.sql_identifier(output_name)}"
        )

    def _index_source_filters(self) -> dict[tuple[str, str], _SourceFilterSpec]:
        filters: dict[tuple[str, str], _SourceFilterSpec] = {}
        for mapping in self.result.mappings:
            mapping_key = self.mapping_ids[id(mapping)]
            instances = {
                normalize_identifier(item.name): item for item in mapping.instances
            }
            sources = {
                normalize_identifier(item.name): item for item in mapping.sources
            }
            transformations = {
                normalize_identifier(item.name): item
                for item in mapping.transformations
            }
            for instance in mapping.instances:
                transformation = transformations.get(_definition_name(instance))
                if (
                    transformation is None
                    or normalize_type_name(transformation.type) != "source qualifier"
                ):
                    continue
                filter_values = [
                    value.strip()
                    for name, value in sorted(transformation.attributes.items())
                    if _attribute_key(name) in {
                        "source filter",
                        "source filter condition",
                    }
                    and value.strip()
                ]
                if not filter_values:
                    continue
                if len(set(filter_values)) > 1:
                    self._record_source_filter_block(
                        mapping_key,
                        instance.name,
                        "SOURCE_QUALIFIER_SOURCE_FILTER_CONFLICT",
                        f"Source Qualifier '{instance.name}' declares conflicting source filters.",
                    )
                    continue
                source_text = filter_values[0]
                try:
                    expression = parse_simple_predicate(source_text)
                except ExpressionSyntaxError as error:
                    self._record_source_filter_block(
                        mapping_key,
                        instance.name,
                        "SOURCE_QUALIFIER_SOURCE_FILTER_PARSE",
                        f"Source Qualifier '{instance.name}' has an unsupported source filter: {error}",
                    )
                    continue

                source_instances = {
                    normalize_identifier(connector.from_instance)
                    for connector in mapping.connectors
                    if normalize_identifier(connector.to_instance)
                    == normalize_identifier(instance.name)
                    and normalize_identifier(connector.from_instance) in instances
                    and _is_source(instances[normalize_identifier(connector.from_instance)])
                }
                source_definitions = {
                    _source_identity(definition): definition
                    for instance_key in source_instances
                    if (
                        definition := sources.get(
                            _definition_name(instances[instance_key])
                        )
                    ) is not None
                }
                if len(source_definitions) != 1:
                    self._record_source_filter_block(
                        mapping_key,
                        instance.name,
                        "SOURCE_QUALIFIER_SOURCE_FILTER_SOURCE",
                        f"Source Qualifier '{instance.name}' source filter must resolve to exactly one source.",
                    )
                    continue
                source_identity, source = next(iter(source_definitions.items()))
                try:
                    for reference in iter_port_references(expression):
                        self._validate_source_filter_column(source, reference)
                except ValueError as error:
                    self._record_source_filter_block(
                        mapping_key,
                        instance.name,
                        "SOURCE_QUALIFIER_SOURCE_FILTER_COLUMN",
                        str(error),
                    )
                    continue
                filters[(mapping_key, normalize_identifier(instance.name))] = _SourceFilterSpec(
                    expression=expression,
                )
        return filters

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
        group_bases = {}
        for identity, source in sources.items():
            group_key = group_keys[identity]
            group_bases.setdefault(
                group_key,
                f"src_{snake_case(source.repository)}_{snake_case(source.folder)}",
            )
        group_names = _unique_names(list(group_bases.items()))
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
                seed_identifier=f"seed_{stage_names[identity]}",
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
            "select cast(null as variant) as __infa2dbt_blocked_model\n"
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
                    data_type = self.dialect.data_type(
                        field.datatype, field.precision, field.scale
                    )
                except UnsupportedDialectType as error:
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
                    f"  cast(src.{self.dialect.sql_identifier(field.name)} as {data_type}) "
                    f"as {self.dialect.sql_identifier(snake_case(field.name))}"
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
                    f"from {{{{ source({_jinja_string(info.group_name)}, {_jinja_string(source.name)}) }}}} as src\n"
                )

    def _source_yml(self) -> str:
        groups: dict[str, list[_SourceInfo]] = defaultdict(list)
        for info in self.source_info.values():
            groups[info.group_name].append(info)
        lines = ["version: 2", "", "sources:"]
        if not groups:
            return "version: 2\n\nsources: []\n"
        for group, infos in sorted(groups.items()):
            sample = infos[0].source
            lines.extend([
                f"  - name: {json.dumps(group)}",
                f"    description: {json.dumps(f'PowerCenter sources from {sample.repository}/{sample.folder}')}",
            ])
            if self.dialect.name == "snowflake":
                lines.extend([
                    '    database: "{{ var(\'source_database\', \'RAW\') }}"',
                    '    schema: "{{ var(\'source_schema\', \'PUBLIC\') }}"',
                ])
            else:
                lines.append("    schema: main")
            lines.extend([
                "    quoting:",
                "      identifier: true",
                "    tables:",
            ])
            for info in sorted(infos, key=lambda item: (item.source.name.lower(), item.identity)):
                lines.extend([
                    f"      - name: {json.dumps(info.source.name)}",
                    f"        description: {json.dumps(f'PowerCenter source {info.source.name}')}",
                ])
                if self.dialect.name == "duckdb":
                    lines.append(
                        f"        identifier: {json.dumps(info.seed_identifier)}"
                    )
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
        if not all_entries:
            return "version: 2\n\nmodels: []\n"
        for name, origin, fields, kind in sorted(all_entries, key=lambda item: item[0]):
            lines.extend([
                f"  - name: {json.dumps(name)}",
                f"    description: {json.dumps(f'{kind.title()} model generated from PowerCenter object {origin}.')}",
            ])
            if fields:
                lines.append("    columns:")
                for item in sorted(fields, key=lambda field: normalize_identifier(field.name)):
                    try:
                        mapped_type = self.dialect.data_type(
                            item.datatype, item.precision, item.scale
                        )
                    except UnsupportedDialectType:
                        mapped_type = "UNSUPPORTED"
                    column_name = snake_case(item.name)
                    type_label = (
                        "Snowflake" if self.dialect.name == "snowflake" else "DuckDB"
                    )
                    lines.extend([
                        f"      - name: {json.dumps(column_name)}",
                        f"        description: {json.dumps(f'PowerCenter field {item.name}; {type_label} type {mapped_type}.')}",
                    ])
        return "\n".join(lines) + "\n"

    def _dbt_files(self) -> None:
        project_name = self.dialect.project_name
        profile_name = self.dialect.profile_name
        vars_section = (
            "vars:\n"
            "  source_database: RAW\n"
            "  source_schema: PUBLIC\n"
            if self.dialect.name == "snowflake" else ""
        )
        self.files["dbt_project.yml"] = (
            f"name: {project_name}\n"
            "version: '1.0.0'\n"
            "config-version: 2\n"
            f"profile: {profile_name}\n"
            "model-paths: ['models']\n"
            "analysis-paths: ['analyses']\n"
            "test-paths: ['tests']\n"
            "seed-paths: ['seeds']\n"
            "macro-paths: ['macros']\n"
            "snapshot-paths: ['snapshots']\n"
            "target-path: 'target'\n"
            "clean-targets: ['target', 'dbt_packages']\n"
            f"{vars_section}"
            "models:\n"
            f"  {project_name}:\n"
            "    +materialized: view\n"
        )
        if self.dialect.name == "snowflake":
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
            self.files["requirements.lock"] = DBT_REQUIREMENTS_LOCK
            self.files["README.md"] = (
                "# Generated Snowflake dbt project\n\n"
                "Generated models use views by default. Review `diagnostics.json` before running "
                "the project; blocked models are intentionally non-functional placeholders.\n\n"
                "Copy `profiles.yml` to your local dbt profiles directory and replace every "
                "placeholder with your own Snowflake connection values. Do not commit credentials.\n\n"
                "Install the fully pinned environment with `python -m pip install -r requirements.lock`. "
                "`requirements.txt` lists the direct dbt dependencies. "
                "Run `dbt parse` from this project directory.\n"
            )
        else:
            self.files["profiles.yml"] = (
                f"{profile_name}:\n"
                "  target: dev\n"
                "  outputs:\n"
                "    dev:\n"
                "      type: duckdb\n"
                "      path: validation.duckdb\n"
                "      schema: main\n"
                "      threads: 1\n"
            )
            self.files["README.md"] = (
                "# Generated DuckDB validation project\n\n"
                "This temporary project uses synthetic seed data for local validation. "
                "It is not the Snowflake download project.\n"
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
        source_filter_blocks = self.source_filter_blocks.get(mapping_key, [])
        if source_filter_blocks:
            remaining_errors = [
                item for item in mapping_errors
                if not any(
                    item["code"] == block.code and item["message"] == block.message
                    for block in source_filter_blocks
                )
            ]
            blocks = list(source_filter_blocks)
            if remaining_errors:
                blocks.append(_Block(
                    "INVALID_MAPPING",
                    f"Mapping '{mapping_key}' has validation errors; its target model is blocked.",
                    target_instance.name,
                ))
            raise _GenerationBlocked(blocks)
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
                    raise _GenerationBlocked(blocks)

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
            source_filter_sql = ""
            source_filter = self.source_filters.get((mapping_key, instance_key))
            if type_name == "source qualifier" and source_filter is not None:
                if parent_relation is None:
                    raise _GenerationBlocked(_Block(
                        "SOURCE_QUALIFIER_SOURCE_FILTER_SOURCE",
                        f"Source filter on '{instance.name}' has no input relation.",
                        instance.name,
                        "Source Filter",
                    ))
                try:
                    source_filter_sql = self.dialect.render_predicate(
                        source_filter.expression,
                        lambda reference: self._source_filter_relation_column(
                            parent_relation,
                            relation_alias,
                            reference,
                        ),
                    )
                except ValueError as error:
                    raise _GenerationBlocked(_Block(
                        "SOURCE_QUALIFIER_SOURCE_FILTER_COLUMN",
                        str(error),
                        instance.name,
                        "Source Filter",
                    )) from error
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
                        return _render_expression(nested, resolve_port, self.dialect)
                    except ExpressionSyntaxError as error:
                        raise _GenerationBlocked(_Block(
                            "INVALID_EXPRESSION",
                            str(error),
                            instance.name,
                            candidate.name,
                        )) from error
                    except UnsupportedDialectFunction as error:
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
                    return (
                        f"{self.dialect.sql_identifier(relation_alias, context='relation')}."
                        f"{self.dialect.sql_identifier(output_name)}"
                    )

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
                        return _render_expression(nested, resolve_port, self.dialect)
                    except ExpressionSyntaxError as error:
                        raise _GenerationBlocked(_Block(
                            "INVALID_EXPRESSION",
                            str(error),
                            instance.name,
                            candidate.name,
                        )) from error
                    except UnsupportedDialectFunction as error:
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
                        return (
                            f"{self.dialect.sql_identifier(relation_alias, context='relation')}."
                            f"{self.dialect.sql_identifier(output_name)}"
                        )
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
                        expression_sql = _render_expression(
                            ast, resolve_port, self.dialect
                        )
                    except ExpressionSyntaxError as error:
                        raise _GenerationBlocked(_Block(
                            "INVALID_EXPRESSION", str(error), instance.name, port.name
                        )) from error
                    except UnsupportedDialectFunction as error:
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
                    f"    {expression_sql} as {self.dialect.sql_identifier(output_name)}"
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
                    where_sql = _render_expression(
                        condition_ast, resolve_port, self.dialect
                    )
                except ExpressionSyntaxError as error:
                    raise _GenerationBlocked(_Block(
                        "INVALID_FILTER_EXPRESSION",
                        str(error),
                        instance.name,
                    )) from error
                except UnsupportedDialectFunction as error:
                    raise _GenerationBlocked(_Block(
                        "UNSUPPORTED_FILTER_FUNCTION",
                        str(error),
                        instance.name,
                    )) from error

            cte_name = snake_case(instance.name)
            if parent_relation is None:
                body = ",\n".join(projections)
                cte_sql = (
                    f"{self.dialect.sql_identifier(cte_name, context='relation')} as "
                    f"(\n  select\n{body}\n)"
                )
            else:
                from_sql = parent_relation.sql
                if parent_relation.is_cte:
                    from_sql = self.dialect.sql_identifier(
                        from_sql, context="relation"
                    )
                body = ",\n".join(projections)
                where_conditions = [
                    condition for condition in (source_filter_sql, where_sql)
                    if condition
                ]
                where_clause = (
                    "\n  where " + " AND ".join(where_conditions)
                    if where_conditions else ""
                )
                cte_sql = (
                    f"{self.dialect.sql_identifier(cte_name, context='relation')} as (\n"
                    f"  select\n{body}\n"
                    f"  from {from_sql} as "
                    f"{self.dialect.sql_identifier(relation_alias, context='relation')}"
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
                self.dialect.data_type(
                    field_item.datatype, field_item.precision, field_item.scale
                )
            except UnsupportedDialectType as error:
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
                f"  {self.dialect.sql_identifier('target_input', context='relation')}."
                f"{self.dialect.sql_identifier(relation_field)} "
                f"as {self.dialect.sql_identifier(target_alias)}"
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
            from_sql = self.dialect.sql_identifier(from_sql, context="relation")
        cte_prefix = "with\n" + ",\n".join(ctes) + "\n\n" if ctes else ""
        target_select = ",\n".join(target_projections)
        return (
            f"{{{{ config(materialized='view') }}}}\n\n"
            f"{cte_prefix}select\n"
            f"{target_select}\n"
            f"from {from_sql} as "
            f"{self.dialect.sql_identifier('target_input', context='relation')}\n"
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
                    blocks.extend(error.blocks)
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
            "warehouse": self.dialect.name,
            "blocked_models": self.blocked_models,
            "diagnostics": self.diagnostics,
        }, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        seed_tables = (
            [
                SeedTable(
                    filename=f"{info.seed_identifier}.csv",
                    identifier=info.seed_identifier,
                    source=info.source,
                )
                for info in self.source_info.values()
            ]
            if self.dialect.name == "duckdb" else []
        )
        return DbtProject(
            files=dict(sorted(self.files.items())),
            diagnostics=self.diagnostics,
            blocked_models=self.blocked_models,
            seed_tables=seed_tables,
        )


def build_project(
    result: ParseResult,
    dialect: str = "snowflake",
) -> DbtProject:
    """Build deterministic dbt files, using Snowflake unless another dialect is selected."""
    return _ProjectBuilder(result, get_dialect(dialect)).build()


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
