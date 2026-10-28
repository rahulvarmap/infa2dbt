from collections import deque

from .models import Mapping


def topological_order(mapping: Mapping) -> list[str]:
    """Stable Kahn ordering, counting instance edges rather than individual ports."""
    names = [instance.name for instance in mapping.instances]
    rank = {name: index for index, name in enumerate(names)}
    if len(names) != len(set(names)):
        raise ValueError(f"Mapping '{mapping.name}' contains duplicate instance names.")
    outgoing: dict[str, set[str]] = {name: set() for name in names}
    indegree = dict.fromkeys(names, 0)
    for connector in mapping.connectors:
        source, target = connector.from_instance, connector.to_instance
        if source not in outgoing or target not in outgoing:
            raise ValueError(
                f"Mapping '{mapping.name}' has a connector referencing an unknown "
                f"instance: {source} → {target}."
            )
        if target not in outgoing[source]:
            outgoing[source].add(target)
            indegree[target] += 1
    ready = deque(name for name in names if indegree[name] == 0)
    result = []
    while ready:
        source = ready.popleft()
        result.append(source)
        for target in sorted(outgoing[source], key=rank.__getitem__):
            indegree[target] -= 1
            if indegree[target] == 0:
                ready.append(target)
    if len(result) != len(names):
        blocked = ", ".join(name for name in names if indegree[name])
        raise ValueError(
            f"Cycle detected in mapping '{mapping.name}'; involved or blocked instances: {blocked}."
        )
    return result
