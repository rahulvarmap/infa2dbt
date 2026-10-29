"""Instance graphs and deterministic, target-to-source field lineage."""

import json
from collections import defaultdict

import networkx as nx

from .models import (
    FieldPair, LineageData, LineageEdge, LineageHop, LineageNode, Mapping,
)
from .identifiers import normalize_identifier, normalize_type_name
from .order import topological_order
from .diagnostics import mapping_identity
from .expressions import (
    ExpressionSyntaxError,
    iter_port_references,
    parse_expression,
)


def mapping_key(mapping: Mapping, mappings: list[Mapping]) -> str:
    return mapping_identity(mapping, mappings)


class LineageGraph:
    def __init__(self, mapping: Mapping):
        # Validate connector endpoints and cycles before constructing a graph.
        topological_order(mapping)
        self.mapping = mapping
        self.graph = nx.DiGraph()
        self.instances = {normalize_identifier(instance.name): instance for instance in mapping.instances}
        self.ports = {}
        self.incoming = defaultdict(list)
        transformations = {normalize_identifier(item.name): item for item in mapping.transformations}
        sources = {normalize_identifier(item.name): item for item in mapping.sources}
        targets = {normalize_identifier(item.name): item for item in mapping.targets}
        for instance in mapping.instances:
            name = normalize_identifier(instance.transformation_name or instance.name)
            kind = normalize_type_name(instance.type)
            definition_kind = normalize_type_name(instance.transformation_type)
            if kind in {"source", "source definition"} or definition_kind == "source definition":
                definition, node_type = sources.get(name), "Source"
            elif kind in {"target", "target definition"} or definition_kind == "target definition":
                definition, node_type = targets.get(name), "Target"
            else:
                definition = transformations.get(name)
                node_type = (
                    definition.type if definition else instance.transformation_type
                ) or "Unknown"
            fields = (
                getattr(definition, "ports", getattr(definition, "fields", []))
                if definition else []
            )
            port_map = {}
            for port in fields:
                port_map.setdefault(normalize_identifier(port.name), port)
            self.ports[normalize_identifier(instance.name)] = port_map
            self.graph.add_node(instance.name, name=instance.name, type=node_type)
        for connector in mapping.connectors:
            source_instance = self.instances[normalize_identifier(connector.from_instance)]
            target_instance = self.instances[normalize_identifier(connector.to_instance)]
            source, target = source_instance.name, target_instance.name
            if not self.graph.has_edge(source, target):
                self.graph.add_edge(source, target, fields=[])
            self.graph[source][target]["fields"].append(
                FieldPair(from_field=connector.from_field, to_field=connector.to_field)
            )
            self.incoming[(normalize_identifier(target), normalize_identifier(connector.to_field))].append(
                (source, connector.from_field)
            )

    def as_model(self, key: str | None = None, scoped: bool = False) -> LineageData:
        key = key if key is not None else self.mapping.name

        def node_id(name):
            return json.dumps([key, name], separators=(",", ":")) if scoped else name

        return LineageData(
            nodes=[
                LineageNode(id=node_id(name), mapping=key, **attributes)
                for name, attributes in self.graph.nodes(data=True)
            ],
            edges=[
                LineageEdge(
                    id=json.dumps([node_id(source), node_id(target)], separators=(",", ":")),
                    source=node_id(source), target=node_id(target), mapping=key,
                    fields=attributes["fields"],
                )
                for source, target, attributes in self.graph.edges(data=True)
            ],
        )

    def trace_field(self, target_instance: str, target_field: str) -> list[LineageHop]:
        instance = self.instances.get(normalize_identifier(target_instance))
        if instance is None:
            raise ValueError(f"Unknown instance: {target_instance}.")
        instance_key = normalize_identifier(instance.name)
        target_field_key = normalize_identifier(target_field)
        known_ports = self.ports[instance_key]
        connector_fields = {
            normalize_identifier(value)
            for c in self.mapping.connectors
            for name, value in [(c.from_instance, c.from_field), (c.to_instance, c.to_field)]
            if normalize_identifier(name) == instance_key
        }
        if target_field_key not in known_ports and target_field_key not in connector_fields:
            raise ValueError(f"Unknown field: {instance.name}.{target_field}.")
        hops, visited, active = [], set(), set()
        # An explicit DFS stack handles long mappings without Python recursion limits.
        stack = [(instance.name, target_field, False)]
        while stack:
            name, field, exiting = stack.pop()
            identity = (normalize_identifier(name), normalize_identifier(field))
            if exiting:
                active.remove(identity)
                visited.add(identity)
                continue
            if identity in active:
                raise ValueError(f"Cycle detected in field lineage at {name}.{field}.")
            if identity in visited:
                continue
            active.add(identity)
            ports = self.ports[normalize_identifier(name)]
            port = ports.get(normalize_identifier(field))
            expression = getattr(port, "expression", "")
            hops.append(LineageHop(
                instance=name, port=port.name if port else field,
                type=self.graph.nodes[name]["type"], expression=expression,
            ))
            upstream = []
            port_type = getattr(port, "porttype", "").upper() if port else ""
            if port and expression and ("OUTPUT" in port_type or "VARIABLE" in port_type):
                variable_ports = [
                    candidate.name for candidate in ports.values()
                    if "VARIABLE" in getattr(candidate, "porttype", "").upper()
                ]
                try:
                    expression_ast = parse_expression(
                        expression,
                        variable_ports=variable_ports,
                    )
                    references = iter_port_references(expression_ast)
                    for reference in references:
                        candidate = ports.get(normalize_identifier(reference.name))
                        if candidate is None:
                            continue
                        candidate_type = getattr(candidate, "porttype", "").upper()
                        if (
                            normalize_identifier(candidate.name) == normalize_identifier(field)
                            and "INPUT" in candidate_type
                            and "OUTPUT" in candidate_type
                        ):
                            upstream.extend(self.incoming[identity])
                        else:
                            upstream.append((name, candidate.name))
                except ExpressionSyntaxError:
                    # Invalid expressions have a structured diagnostic; retain
                    # only the explicit connector path as a conservative fallback.
                    upstream.extend(self.incoming[identity])
            else:
                upstream.extend(self.incoming[identity])
            stack.append((name, field, True))
            stack.extend((n, p, False) for n, p in reversed(upstream))
        return hops


def build_lineage(mapping: Mapping) -> LineageData:
    return LineageGraph(mapping).as_model()


def trace_field(mapping: Mapping, target_instance: str, target_field: str) -> list[LineageHop]:
    return LineageGraph(mapping).trace_field(target_instance, target_field)
