from __future__ import annotations

import json
import base64
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from api.parser import parse_powercenter_xml, topological_order
from api.parser.diagnostics import validate_mapping, validate_parse_result
from api.parser.identifiers import normalize_identifier
from api.parser.lineage import LineageGraph, mapping_key
from api.parser.models import Diagnostic, LineageData, ParseResponse, TraceResponse
from api.request_limits import MAX_MAPPINGS
from api.dbt import build_project, validate_project, zip_project
from api.explainer import explain_result


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
            diagnostics = validate_parse_result(result)
            for mapping in result.mappings:
                key = mapping_key(mapping, result.mappings)
                if key in orders:
                    raise ValueError(f"Duplicate mapping identity: {key}.")
                mapping_diagnostics = [d for d in diagnostics if d.mapping == key]
                if any(d.severity == "error" for d in mapping_diagnostics):
                    continue
                try:
                    orders[key] = topological_order(mapping)
                    graph = LineageGraph(mapping).as_model(
                        key, scoped=len(result.mappings) > 1
                    )
                    lineage.nodes.extend(graph.nodes)
                    lineage.edges.extend(graph.edges)
                except ValueError as error:
                    diagnostics.append(Diagnostic(
                        severity="error",
                        code="INVALID_MAPPING_GRAPH",
                        message=str(error),
                        mapping=key,
                    ))
            response = ParseResponse(
                **result.model_dump(),
                execution_order=orders,
                lineage=lineage,
                diagnostics=diagnostics,
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
                    or normalize_identifier(mapping_key(item, result.mappings))
                    == normalize_identifier(selected_mapping)
                )
                and any(
                    normalize_identifier(instance.name) == normalize_identifier(target_instance)
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
            selected_key = mapping_key(selected, result.mappings)
            if any(d.severity == "error" for d in validate_mapping(selected, selected_key)):
                raise ValueError(f"Mapping '{selected_key}' has validation errors; field lineage is unavailable.")
            response = TraceResponse(
                mapping=selected_key,
                hops=LineageGraph(selected).trace_field(
                    target_instance, target_field
                ),
            )
            return {"status_code": 200, "content": response.model_dump()}

        if mode in {"generate", "generate_files"}:
            dialect = fields.get("dialect", "snowflake")
            project = build_project(result, dialect=dialect)
            if mode == "generate_files":
                return {
                    "status_code": 200,
                    "content": {
                        "dialect": dialect,
                        "files": project.files,
                        "blocked_models": project.blocked_models,
                        "diagnostics": project.diagnostics,
                    },
                }
            archive = zip_project(project.files)
            return {
                "status_code": 200,
                "content": {
                    "filename": f"infa2dbt_{dialect}.zip",
                    "zip_base64": base64.b64encode(archive).decode("ascii"),
                    "blocked_models": project.blocked_models,
                    "diagnostics": project.diagnostics,
                },
            }

        if mode == "explain":
            dialect = fields.get("dialect", "snowflake")
            project = build_project(result, dialect=dialect)
            return {
                "status_code": 200,
                "content": {
                    "dialect": dialect,
                    "explanations": explain_result(result, project),
                },
            }

        if mode == "validate":
            return {
                "status_code": 200,
                "content": validate_project(result),
            }

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
