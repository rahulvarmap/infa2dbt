"""Inspection and column-lineage capabilities, not dbt compilation support."""

from dataclasses import dataclass
from typing import Literal

from .identifiers import normalize_type_name


@dataclass(frozen=True)
class TransformationCapability:
    level: Literal["supported", "partial", "metadata_only"]
    diagnostic_code: str | None = None
    message: str = ""


def _partial(code: str, message: str) -> TransformationCapability:
    return TransformationCapability("partial", code, message)


def _metadata(message: str) -> TransformationCapability:
    return TransformationCapability(
        "metadata_only",
        "UNMODELED_TRANSFORMATION_SEMANTICS",
        message,
    )


CAPABILITY_REGISTRY: dict[str, TransformationCapability] = {
    "source": TransformationCapability("supported"),
    "source definition": TransformationCapability("supported"),
    "target": TransformationCapability("supported"),
    "target definition": TransformationCapability("supported"),
    "source qualifier": _partial(
        "PARTIAL_SOURCE_QUALIFIER",
        "Source-query behavior such as SQL overrides and user-defined joins is not represented in lineage.",
    ),
    "expression": _partial(
        "PARTIAL_EXPRESSION_LINEAGE",
        "Expression lineage follows declared input ports and local-variable chains; other expression semantics are not interpreted.",
    ),
    "filter": _partial(
        "PARTIAL_FILTER_LINEAGE",
        "Filter predicates and row-level effects are not represented in column-value lineage.",
    ),
    "joiner": _partial(
        "PARTIAL_JOINER_LINEAGE",
        "Supported equality join keys and row-retention types are generated in dbt; column-value lineage does not model row-level join effects.",
    ),
    "lookup": _partial(
        "PARTIAL_LOOKUP_LINEAGE",
        "Connected lookup conditions and default values are generated for the supported equality-match subset; row-selection behavior is not represented in column-value lineage.",
    ),
    "lookup procedure": _metadata("Lookup source, conditions, defaults, and match behavior are not interpreted."),
    "aggregator": _partial(
        "PARTIAL_AGGREGATOR_LINEAGE",
        "Supported aggregate expressions are generated in dbt; grouping and row-reduction effects are not represented in column-value lineage.",
    ),
    "router": _metadata("Router group conditions and branch semantics are not interpreted."),
    "union": _metadata("Union input alignment and duplicate semantics are not interpreted."),
    "sorter": _metadata("Sort order and distinctness semantics are not interpreted."),
    "rank": _metadata("Ranking criteria, ties, and row effects are not interpreted."),
    "sequence generator": _metadata("Sequence state and generated values are not interpreted."),
    "update strategy": _metadata("Insert, update, delete, and reject semantics are not interpreted."),
    "normalizer": _metadata("Row expansion and normalization semantics are not interpreted."),
    "transaction control": _metadata("Transaction and commit behavior is not interpreted."),
    "sql": _metadata("Embedded SQL behavior is not interpreted."),
    "stored procedure": _metadata("Stored-procedure behavior and side effects are not interpreted."),
    "java": _metadata("Java transformation code and side effects are not interpreted."),
}


def capability_for(type_name: str) -> TransformationCapability:
    key = normalize_type_name(type_name)
    capability = CAPABILITY_REGISTRY.get(key)
    if capability is not None:
        return capability
    return TransformationCapability(
        "metadata_only",
        "UNKNOWN_TRANSFORMATION_TYPE",
        f"No capability is registered for transformation type '{type_name}'; it is preserved as metadata but its semantics are not interpreted.",
    )
