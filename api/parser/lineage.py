"""Instance graphs and deterministic, target-to-source field lineage."""

import json
import re
from collections import defaultdict

import networkx as nx

from .models import (
    FieldPair, LineageData, LineageEdge, LineageHop, LineageNode, Mapping,
)
from .order import topological_order


def mapping_key(mapping: Mapping, mappings: list[Mapping]) -> str:
    if sum(item.name == mapping.name for item in mappings) > 1:
        return f"{mapping.repository}/{mapping.folder}/{mapping.name}"
    return mapping.name


class LineageGraph:
    def __init__(self, mapping: Mapping):
        # Validate connector endpoints and cycles before constructing a graph.
        topological_order(mapping)
        self.mapping = mapping
        self.graph = nx.DiGraph()
        self.instances = {instance.name.casefold(): instance for instance in mapping.instances}
        self.ports = {}
        self.incoming = defaultdict(list)
        transformations = {item.name.casefold(): item for item in mapping.transformations}
        sources = {item.name.casefold(): item for item in mapping.sources}
        targets = {item.name.casefold(): item for item in mapping.targets}
        for instance in mapping.instances:
            name = (instance.transformation_name or instance.name).casefold()
            kind = instance.type.casefold()
            if kind == "source":
                definition, node_type = sources.get(name), "Source"
            elif kind == "target":
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
            self.ports[instance.name.casefold()] = {p.name.casefold(): p for p in fields}
            self.graph.add_node(instance.name, name=instance.name, type=node_type)
        for connector in mapping.connectors:
            source, target = connector.from_instance, connector.to_instance
            if not self.graph.has_edge(source, target):
                self.graph.add_edge(source, target, fields=[])
            self.graph[source][target]["fields"].append(
                FieldPair(from_field=connector.from_field, to_field=connector.to_field)
            )
            self.incoming[(target.casefold(), connector.to_field.casefold())].append(
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
        instance = self.instances.get(target_instance.casefold())
        if instance is None:
            raise ValueError(f"Unknown instance: {target_instance}.")
        known_ports = self.ports[instance.name.casefold()]
        connector_fields = {
            value.casefold()
            for c in self.mapping.connectors
            for name, value in [(c.from_instance, c.from_field), (c.to_instance, c.to_field)]
            if name.casefold() == instance.name.casefold()
        }
        if target_field.casefold() not in known_ports and target_field.casefold() not in connector_fields:
            raise ValueError(f"Unknown field: {instance.name}.{target_field}.")
        hops, visited, active = [], set(), set()
        # An explicit DFS stack handles long mappings without Python recursion limits.
        stack = [(instance.name, target_field, False)]
        while stack:
            name, field, exiting = stack.pop()
            identity = (name.casefold(), field.casefold())
            if exiting:
                active.remove(identity)
                visited.add(identity)
                continue
            if identity in active:
                raise ValueError(f"Cycle detected in field lineage at {name}.{field}.")
            if identity in visited:
                continue
            active.add(identity)
            ports = self.ports[name.casefold()]
            port = ports.get(field.casefold())
            expression = getattr(port, "expression", "")
            hops.append(LineageHop(
                instance=name, port=port.name if port else field,
                type=self.graph.nodes[name]["type"], expression=expression,
            ))
            upstream = []
            if port and "OUTPUT" in getattr(port, "porttype", "").upper() and expression:
                # Quoted string literals are values, not references to similarly named ports.
                code = re.sub(r"'(?:''|[^'])*'", " ", expression)
                references = []
                for candidate in ports.values():
                    if "INPUT" not in getattr(candidate, "porttype", "").upper():
                        continue
                    match = re.search(
                        rf"(?<!\w){re.escape(candidate.name)}(?!\w)", code, re.IGNORECASE
                    )
                    if match:
                        references.append((match.start(), candidate))
                for _, candidate in sorted(references, key=lambda item: item[0]):
                    if candidate.name.casefold() == field.casefold():
                        upstream.extend(self.incoming[identity])
                    else:
                        upstream.append((name, candidate.name))
            else:
                upstream.extend(self.incoming[identity])
            stack.append((name, field, True))
            stack.extend((n, p, False) for n, p in reversed(upstream))
        return hops


def build_lineage(mapping: Mapping) -> LineageData:
    return LineageGraph(mapping).as_model()


def trace_field(mapping: Mapping, target_instance: str, target_field: str) -> list[LineageHop]:
    return LineageGraph(mapping).trace_field(target_instance, target_field)
