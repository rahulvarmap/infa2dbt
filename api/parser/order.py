from collections import deque

from .identifiers import normalize_identifier
from .models import Mapping


def topological_order(mapping: Mapping) -> list[str]:
    """Stable Kahn ordering, counting instance edges rather than individual ports."""
    names = [instance.name for instance in mapping.instances]
    keys = [normalize_identifier(name) for name in names]
    original_names = dict(zip(keys, names))
    rank = {key: index for index, key in enumerate(keys)}
    if len(keys) != len(set(keys)):
        raise ValueError(f"Mapping '{mapping.name}' contains duplicate instance names.")
    outgoing: dict[str, set[str]] = {key: set() for key in keys}
    indegree = dict.fromkeys(keys, 0)
    for connector in mapping.connectors:
        source = normalize_identifier(connector.from_instance)
        target = normalize_identifier(connector.to_instance)
        if source not in outgoing or target not in outgoing:
            raise ValueError(
                f"Mapping '{mapping.name}' has a connector referencing an unknown "
                f"instance: {source} → {target}."
            )
        if target not in outgoing[source]:
            outgoing[source].add(target)
            indegree[target] += 1
    ready = deque(key for key in keys if indegree[key] == 0)
    result = []
    while ready:
        source = ready.popleft()
        result.append(original_names[source])
        for target in sorted(outgoing[source], key=rank.__getitem__):
            indegree[target] -= 1
            if indegree[target] == 0:
                ready.append(target)
    if len(result) != len(names):
        blocked = ", ".join(original_names[key] for key in keys if indegree[key])
        raise ValueError(
            f"Cycle detected in mapping '{mapping.name}'; involved or blocked instances: {blocked}."
        )
    return result
