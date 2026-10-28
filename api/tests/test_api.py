import asyncio

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
    assert response.status_code == 400
    assert "Cycle detected" in response.json()["error"]


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
