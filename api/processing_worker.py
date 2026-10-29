from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from api.parser import parse_powercenter_xml, topological_order
from api.parser.lineage import LineageGraph, mapping_key
from api.parser.models import LineageData, ParseResponse, TraceResponse
from api.request_limits import MAX_MAPPINGS


class MappingLimitExceeded(ValueError):
    pass


def process_xml_request(
    mode: str, xml_bytes: bytes, fields: dict[str, str]
) -> dict[str, Any]:
    try:
        result = parse_powercenter_xml(xml_bytes)
        if len(result.mappings) > MAX_MAPPINGS:
            raise MappingLimitExceeded(
                f"XML may contain no more than {MAX_MAPPINGS} mappings."
            )

        if mode == "parse":
            orders = {}
            lineage = LineageData()
            for mapping in result.mappings:
                key = mapping_key(mapping, result.mappings)
                if key in orders:
                    raise ValueError(f"Duplicate mapping identity: {key}.")
                orders[key] = topological_order(mapping)
                graph = LineageGraph(mapping).as_model(
                    key, scoped=len(result.mappings) > 1
                )
                lineage.nodes.extend(graph.nodes)
                lineage.edges.extend(graph.edges)
            response = ParseResponse(
                **result.model_dump(),
                execution_order=orders,
                lineage=lineage,
            )
            return {"status_code": 200, "content": response.model_dump()}

        if mode == "trace":
            target_instance = fields["target_instance"]
            target_field = fields["target_field"]
            selected_mapping = fields.get("mapping")
            matches = [
                item
                for item in result.mappings
                if (
                    selected_mapping is None
                    or mapping_key(item, result.mappings) == selected_mapping
                )
                and any(
                    instance.name.casefold() == target_instance.casefold()
                    for instance in item.instances
                )
            ]
            if not matches:
                raise ValueError("Target instance or mapping not found.")
            if len(matches) != 1:
                raise ValueError(
                    "Target instance is ambiguous; provide the mapping identity."
                )
            selected = matches[0]
            response = TraceResponse(
                mapping=mapping_key(selected, result.mappings),
                hops=LineageGraph(selected).trace_field(
                    target_instance, target_field
                ),
            )
            return {"status_code": 200, "content": response.model_dump()}

        return {"status_code": 500, "content": {"error": "Invalid processing mode."}}
    except MappingLimitExceeded as error:
        return {"status_code": 413, "content": {"error": str(error)}}
    except ValueError as error:
        return {"status_code": 400, "content": {"error": str(error)}}


def main() -> int:
    header = sys.stdin.buffer.readline(16 * 1024)
    try:
        request = json.loads(header)
    except (UnicodeDecodeError, json.JSONDecodeError):
        print(json.dumps({"status_code": 400, "content": {"error": "Invalid request."}}))
        return 0

    xml_bytes = sys.stdin.buffer.read()
    response = process_xml_request(
        request.get("mode", ""), xml_bytes, request.get("fields", {})
    )
    sys.stdout.write(json.dumps(response, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
