import asyncio
import json
import zipfile
from io import BytesIO

import httpx
import pytest

from artifacts.infa2dbt.api.main import MAX_FILE_SIZE, app
from api.tests.test_parser import EXPECTED_ORDER, SAMPLE


def request(method, path, **kwargs):
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            return await client.request(method, path, **kwargs)
    return asyncio.run(run())


@pytest.mark.parametrize("path", ["/parse", "/api/parse"])
def test_parse_sample_upload(path):
    response = request("POST", path, files={"file": ("sample.xml", SAMPLE.read_bytes(), "text/xml")})
    assert response.status_code == 200
    result = response.json()
    assert len(result["sources"]) == len(result["targets"]) == 1
    assert len(result["mappings"][0]["transformations"]) == 3
    assert len(result["mappings"][0]["instances"]) == 5
    assert len(result["mappings"][0]["connectors"]) == 35
    assert result["execution_order"]["m_member_enrollment"] == EXPECTED_ORDER
    assert len(result["lineage"]["nodes"]) == 5
    assert len(result["lineage"]["edges"]) == 4


@pytest.mark.parametrize("path", ["/upload", "/parse"])
def test_same_upload_validation(path):
    assert request("POST", path, files={"file": ("bad.txt", b"xml")}).status_code == 400
    response = request("POST", path, files={"file": ("big.xml", b"x" * (MAX_FILE_SIZE + 1))})
    assert response.status_code == 413
    assert "5 MB" in response.json()["error"]
    assert request("POST", path).status_code == 422


def test_malformed_and_cycle_errors():
    response = request("POST", "/parse", files={"file": ("bad.xml", b"<broken")})
    assert response.status_code == 400
    assert "Invalid XML" in response.json()["error"]
    data = SAMPLE.read_bytes().replace(
        b"</MAPPING>",
        b'<CONNECTOR FROMINSTANCE="DIM_MEMBER" FROMFIELD="x" TOINSTANCE="MEMBER_ENROLLMENT" TOFIELD="x"/></MAPPING>',
    )
    response = request("POST", "/parse", files={"file": ("cycle.xml", data)})
    assert response.status_code == 200
    assert any(
        item["code"] == "DEPENDENCY_CYCLE" and item["severity"] == "error"
        for item in response.json()["diagnostics"]
    )


def test_try_sample_without_upload_and_unknown_sample():
    samples = request("GET", "/api/samples").json()
    assert {"filename": SAMPLE.name, "size_bytes": SAMPLE.stat().st_size} in samples
    response = request("POST", f"/api/samples/{SAMPLE.name}/parse")
    assert response.status_code == 200
    assert response.json()["execution_order"]["m_member_enrollment"] == EXPECTED_ORDER
    assert request("POST", "/api/samples/not-present.xml/parse").status_code == 404
    assert request("POST", "/api/samples/%2E%2E%2Fpyproject.toml/parse").status_code in {404, 405}


def test_upload_metadata_unchanged():
    response = request("POST", "/upload", files={"file": ("sample.xml", SAMPLE.read_bytes())})
    assert response.json() == {"filename": "sample.xml", "size_bytes": SAMPLE.stat().st_size}


def test_duplicate_mapping_names_are_context_qualified():
    xml = (
        b'<POWERMART><REPOSITORY NAME="r">'
        b'<FOLDER NAME="a"><MAPPING NAME="m"/></FOLDER>'
        b'<FOLDER NAME="b"><MAPPING NAME="m"/></FOLDER>'
        b'</REPOSITORY></POWERMART>'
    )
    response = request("POST", "/parse", files={"file": ("multiple.xml", xml)})
    assert response.json()["execution_order"] == {"r/a/m": [], "r/b/m": []}


@pytest.mark.parametrize("path", ["/lineage/trace", "/api/lineage/trace"])
@pytest.mark.parametrize("use_sample", [False, True])
def test_trace_upload_or_sample(path, use_sample):
    data = {"target_instance": "DIM_MEMBER", "target_field": "GENDER_DESC"}
    kwargs = {"data": data}
    if use_sample:
        data["sample_name"] = SAMPLE.name
    else:
        kwargs["files"] = {"file": ("sample.xml", SAMPLE.read_bytes())}
    response = request("POST", path, **kwargs)
    assert response.status_code == 200
    result = response.json()
    assert result["mapping"] == "m_member_enrollment"
    assert len(result["hops"]) == 6
    assert result["hops"][-1]["instance"] == "MEMBER_ENROLLMENT"
    assert "DECODE" in result["hops"][2]["expression"]


def test_trace_validation_and_no_sample_path_traversal():
    data = {"target_instance": "DIM_MEMBER", "target_field": "GENDER_DESC"}
    assert request("POST", "/api/lineage/trace", data=data).status_code == 400
    both = {**data, "sample_name": SAMPLE.name}
    assert request("POST", "/api/lineage/trace", data=both, files={
        "file": ("sample.xml", SAMPLE.read_bytes()),
    }).status_code == 400
    for sample_name in ["missing.xml", "../pyproject.toml", str(SAMPLE.resolve())]:
        response = request("POST", "/api/lineage/trace", data={**data, "sample_name": sample_name})
        assert response.status_code == 404
    assert request("POST", "/api/lineage/trace", data=data, files={
        "file": ("not.txt", b"bad"),
    }).status_code == 400
    assert request("POST", "/api/lineage/trace", data=data, files={
        "file": ("huge.xml", b"x" * (MAX_FILE_SIZE + 1)),
    }).status_code == 413
    assert request("POST", "/api/lineage/trace", data=data, files={
        "file": ("broken.xml", b"<bad"),
    }).status_code == 400
    assert request("POST", "/api/lineage/trace", data={"sample_name": SAMPLE.name}).status_code == 422
    for patch in [{"target_field": "missing"}, {"target_instance": "missing"}, {"mapping": "missing"}]:
        response = request("POST", "/api/lineage/trace", data={
            **data, "sample_name": SAMPLE.name, **patch,
        })
        assert response.status_code == 400


def test_multi_mapping_graph_and_trace_disambiguation():
    import re

    contents = SAMPLE.read_bytes()
    mapping_xml = re.search(rb"<MAPPING\b.*?</MAPPING>", contents, re.DOTALL).group()
    second = mapping_xml.replace(b'NAME="m_member_enrollment"', b'NAME="second_mapping"', 1)
    contents = contents.replace(b"</FOLDER>", second + b"</FOLDER>")
    files = {"file": ("multiple.xml", contents)}
    parsed = request("POST", "/api/parse", files=files).json()
    graph = parsed["lineage"]
    assert len(graph["nodes"]) == len({n["id"] for n in graph["nodes"]}) == 10
    assert len(graph["edges"]) == 8
    data = {"target_instance": "DIM_MEMBER", "target_field": "FULL_NAME"}
    assert request("POST", "/api/lineage/trace", files=files, data=data).status_code == 400
    response = request("POST", "/api/lineage/trace", files=files, data={
        **data, "mapping": "second_mapping",
    })
    assert response.status_code == 200
    assert response.json()["mapping"] == "second_mapping"
    source_fields = {h["port"] for h in response.json()["hops"] if h["type"] == "Source"}
    assert source_fields == {"FIRST_NAME", "LAST_NAME"}


def test_generate_dbt_project_from_sample_returns_generated_project_zip():
    response = request(
        "POST",
        "/api/dbt/project",
        data={"sample_name": SAMPLE.name},
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/zip")
    with zipfile.ZipFile(BytesIO(response.content)) as archive:
        assert "models/sources.yml" in archive.namelist()
        assert "models/schema.yml" in archive.namelist()
        assert "dbt_project.yml" in archive.namelist()
        diagnostics = json.loads(archive.read("diagnostics.json"))
        assert diagnostics["blocked_models"] == []
        target = archive.read("models/marts/dim_member.sql").decode()
        assert not target.startswith("-- BLOCKED MODEL: dim_member")
        assert "MONTHS_BETWEEN(" in target
        staging = archive.read("models/staging/stg_member_enrollment.sql").decode()
        assert "where" not in staging.lower()
        assert "where (__in.status_cd IS NOT NULL)" in target

        preview = request(
            "POST",
            "/api/dbt/project/files",
            data={"sample_name": SAMPLE.name, "dialect": "snowflake"},
        )
        assert preview.status_code == 200
        preview_payload = preview.json()
        assert preview_payload["dialect"] == "snowflake"
        assert preview_payload["files"] == {
            path: archive.read(path).decode("utf-8")
            for path in archive.namelist()
        }


def test_generate_dbt_project_from_upload():
    response = request(
        "POST",
        "/dbt/project",
        files={"file": (SAMPLE.name, SAMPLE.read_bytes(), "text/xml")},
    )
    assert response.status_code == 200
    with zipfile.ZipFile(BytesIO(response.content)) as archive:
        assert "models/staging/stg_member_enrollment.sql" in archive.namelist()
        assert "requirements.txt" in archive.namelist()
        assert "dbt-core==1.12.6" in archive.read("requirements.txt").decode()


def test_duckdb_project_preview_matches_the_download_zip():
    request_data = {"sample_name": SAMPLE.name, "dialect": "duckdb"}
    downloaded = request("POST", "/api/dbt/project", data=request_data)
    preview = request("POST", "/api/dbt/project/files", data=request_data)

    assert downloaded.status_code == 200
    assert "infa2dbt_duckdb.zip" in downloaded.headers["content-disposition"]
    assert preview.status_code == 200
    preview_payload = preview.json()
    assert preview_payload["dialect"] == "duckdb"
    with zipfile.ZipFile(BytesIO(downloaded.content)) as archive:
        assert preview_payload["files"] == {
            path: archive.read(path).decode("utf-8")
            for path in archive.namelist()
        }
