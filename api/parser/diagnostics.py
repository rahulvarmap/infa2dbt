"""Reference validation and structured completeness diagnostics."""

from collections import Counter

from .capabilities import capability_for
from .identifiers import normalize_identifier, normalize_type_name
from .models import Diagnostic, Mapping, ParseResult
from .order import topological_order

_SOURCE_TYPES = {"source", "source definition"}
_TARGET_TYPES = {"target", "target definition"}


def mapping_identity(mapping: Mapping, mappings: list[Mapping]) -> str:
    if sum(normalize_identifier(item.name) == normalize_identifier(mapping.name) for item in mappings) > 1:
        return f"{mapping.repository}/{mapping.folder}/{mapping.name}"
    return mapping.name


def _instance_kind(instance) -> str:
    kind = normalize_type_name(instance.type)
    transformation_type = normalize_type_name(instance.transformation_type)
    if kind in _SOURCE_TYPES or transformation_type == "source definition":
        return "source"
    if kind in _TARGET_TYPES or transformation_type == "target definition":
        return "target"
    return "transformation"


def _definitions_by_name(definitions):
    result = {}
    duplicates = set()
    for definition in definitions:
        key = normalize_identifier(definition.name)
        if key in result:
            duplicates.add(key)
        else:
            result[key] = definition
    return result, duplicates


def validate_mapping(mapping: Mapping, identity: str) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []

    def add(severity, code, message, instance=None, field=None):
        diagnostics.append(Diagnostic(
            severity=severity,
            code=code,
            message=message,
            mapping=identity,
            instance=instance,
            field=field,
        ))

    sources, duplicate_sources = _definitions_by_name(mapping.sources)
    targets, duplicate_targets = _definitions_by_name(mapping.targets)
    transformations, duplicate_transformations = _definitions_by_name(mapping.transformations)

    for key in sorted(duplicate_sources):
        add("error", "DUPLICATE_SOURCE_DEFINITION", f"Source definition '{key}' is ambiguous.")
    for key in sorted(duplicate_targets):
        add("error", "DUPLICATE_TARGET_DEFINITION", f"Target definition '{key}' is ambiguous.")
    for key in sorted(duplicate_transformations):
        add("error", "DUPLICATE_TRANSFORMATION_DEFINITION", f"Transformation definition '{key}' is ambiguous.")

    instances = {}
    duplicate_instances = set()
    for instance in mapping.instances:
        key = normalize_identifier(instance.name)
        if not key:
            add("error", "EMPTY_INSTANCE_NAME", "An instance has no name.")
        elif key in instances:
            duplicate_instances.add(key)
        else:
            instances[key] = instance
    for key in sorted(duplicate_instances):
        add("error", "DUPLICATE_INSTANCE", f"Instance name '{key}' is ambiguous.")

    instance_ports = {}
    for instance in mapping.instances:
        instance_key = normalize_identifier(instance.name)
        kind = _instance_kind(instance)
        definition_name = normalize_identifier(instance.transformation_name or instance.name)
        if kind == "source":
            definition = sources.get(definition_name)
            label = "source"
        elif kind == "target":
            definition = targets.get(definition_name)
            label = "target"
        else:
            definition = transformations.get(definition_name)
            label = "transformation"

        if definition is None:
            code = f"UNRESOLVED_{label.upper()}_DEFINITION"
            add(
                "error",
                code,
                f"Instance '{instance.name}' references an unknown {label} definition '{instance.transformation_name or instance.name}'.",
                instance=instance.name,
            )
            instance_ports[instance_key] = {}
            continue

        ports = getattr(definition, "ports", getattr(definition, "fields", []))
        port_index = {}
        duplicate_ports = set()
        for port in ports:
            port_key = normalize_identifier(port.name)
            if port_key in port_index:
                duplicate_ports.add(port_key)
            else:
                port_index[port_key] = port
        for port_key in sorted(duplicate_ports):
            add(
                "error",
                "DUPLICATE_PORT",
                f"Definition '{definition.name}' declares port '{port_key}' more than once.",
                instance=instance.name,
                field=port_key,
            )
        instance_ports[instance_key] = port_index

        if kind == "transformation":
            capability = capability_for(definition.type)
            if capability.diagnostic_code:
                add(
                    "warning",
                    capability.diagnostic_code,
                    capability.message,
                    instance=instance.name,
                )

    for connector in mapping.connectors:
        endpoints = (
            ("from", connector.from_instance, connector.from_field),
            ("to", connector.to_instance, connector.to_field),
        )
        for direction, instance_name, field_name in endpoints:
            instance_key = normalize_identifier(instance_name)
            field_key = normalize_identifier(field_name)
            if instance_key not in instances:
                add(
                    "error",
                    "UNRESOLVED_CONNECTOR_INSTANCE",
                    f"Connector {direction} endpoint references unknown instance '{instance_name}'.",
                    instance=instance_name,
                    field=field_name,
                )
            elif field_key not in instance_ports.get(instance_key, {}):
                add(
                    "error",
                    "UNDECLARED_PORT",
                    f"Connector {direction} endpoint '{instance_name}.{field_name}' is not declared by its definition.",
                    instance=instance_name,
                    field=field_name,
                )

    try:
        topological_order(mapping)
    except ValueError as error:
        if "Cycle detected" in str(error):
            add(
                "error",
                "DEPENDENCY_CYCLE",
                str(error),
            )

    return diagnostics


def validate_parse_result(result: ParseResult) -> list[Diagnostic]:
    if not result.mappings:
        return [Diagnostic(
            severity="warning",
            code="EMPTY_EXPORT",
            message="The XML contains no mappings to inspect.",
            mapping="",
        )]
    identities = [mapping_identity(mapping, result.mappings) for mapping in result.mappings]
    ambiguous_identities = {
        key for key, count in Counter(normalize_identifier(value) for value in identities).items()
        if count > 1
    }
    diagnostics = []
    for mapping, identity in zip(result.mappings, identities):
        diagnostics.extend(validate_mapping(mapping, identity))
        if normalize_identifier(identity) in ambiguous_identities:
            diagnostics.append(Diagnostic(
                severity="error",
                code="DUPLICATE_MAPPING_IDENTITY",
                message=f"More than one mapping resolves to identifier '{identity}'.",
                mapping=identity,
            ))
    return diagnostics
