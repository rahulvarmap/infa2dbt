import asyncio
import os

import httpx
import pytest

from api import request_limits
from api.tests.test_api import request
from api.tests.test_parser import SAMPLE
from artifacts.infa2dbt.api import main


def test_liveness_and_readiness_are_separate(tmp_path, monkeypatch):
    assert request("GET", "/api/health").json() == {"status": "ok"}
    assert request("GET", "/api/ready").json() == {"status": "ready"}
    monkeypatch.setattr(main, "SAMPLES_DIR", tmp_path)
    assert request("GET", "/api/ready").status_code == 503
    assert request("GET", "/api/health").status_code == 200


def test_readiness_checks_parser_dependencies(monkeypatch):
    original = main.importlib.import_module

    def unavailable(name):
        if name == "api.parser.lineage":
            raise ImportError("dependency unavailable")
        return original(name)

    monkeypatch.setattr(main.importlib, "import_module", unavailable)
    response = request("GET", "/ready")
    assert response.status_code == 503
    assert response.json()["checks"]["dependencies"] == "unavailable"


@pytest.mark.parametrize("chunked", [False, True])
def test_body_is_bounded_before_multipart_parsing(chunked, monkeypatch):
    async def must_not_parse(*args, **kwargs):
        raise AssertionError("Oversized bodies must not reach multipart parsing")

    monkeypatch.setattr(main, "read_xml_upload", must_not_parse)
    oversized = b"x" * (request_limits.MAX_REQUEST_BODY_SIZE + 1)

    async def run():
        async def chunks():
            for offset in range(0, len(oversized), 64 * 1024):
                yield oversized[offset:offset + 64 * 1024]

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main.app), base_url="http://test"
        ) as client:
            return await client.post(
                "/api/parse",
                content=chunks() if chunked else oversized,
                headers={"content-type": "multipart/form-data; boundary=example"},
            )

    response = asyncio.run(run())
    assert response.status_code == 413
    assert "Request body" in response.json()["error"]


@pytest.mark.parametrize("path", ["/upload", "/api/parse", "/api/lineage/trace"])
def test_only_one_file_is_accepted(path):
    response = request(
        "POST",
        path,
        files=[
            ("file", ("first.xml", b"<POWERMART/>")),
            ("other", ("second.xml", b"<POWERMART/>")),
        ],
        data={"target_instance": "DIM_MEMBER", "target_field": "FULL_NAME"},
    )
    assert response.status_code == 413
    assert "at most one" in response.json()["error"]


def test_exact_file_limit_and_cleanup(monkeypatch):
    closed = []
    original_close = main.UploadFile.close

    async def close_and_record(file):
        await original_close(file)
        closed.append(file.file.closed)

    monkeypatch.setattr(main.UploadFile, "close", close_and_record)
    response = request(
        "POST", "/upload",
        files={"file": ("boundary.xml", b"x" * request_limits.MAX_FILE_SIZE)},
    )
    assert response.status_code == 200
    assert response.json()["size_bytes"] == request_limits.MAX_FILE_SIZE
    response = request(
        "POST", "/upload",
        files={"file": ("too-large.xml", b"x" * (request_limits.MAX_FILE_SIZE + 1))},
    )
    assert response.status_code == 413
    assert closed and all(closed)


@pytest.mark.parametrize("count,status", [(50, 200), (51, 413)])
def test_mapping_limit(count, status):
    mappings = b"".join(
        f'<MAPPING NAME="mapping_{index}"/>'.encode() for index in range(count)
    )
    contents = (
        b'<POWERMART><REPOSITORY NAME="repo"><FOLDER NAME="folder">'
        + mappings
        + b"</FOLDER></REPOSITORY></POWERMART>"
    )
    response = request(
        "POST", "/parse", files={"file": ("mappings.xml", contents)}
    )
    assert response.status_code == status
    if status == 200:
        assert len(response.json()["mappings"]) == count
    else:
        assert "50 mappings" in response.json()["error"]


def test_busy_requests_timeout_kills_worker_and_releases_slot(tmp_path, monkeypatch):
    pid_file = tmp_path / "worker.pid"
    worker = tmp_path / "slow_worker.py"
    worker.write_text(
        "import os, sys, time\n"
        "from pathlib import Path\n"
        "sys.stdin.buffer.read()\n"
        f"Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
        "time.sleep(60)\n"
    )
    monkeypatch.setattr(main, "WORKER_SCRIPT", worker)
    monkeypatch.setattr(request_limits, "PROCESSING_TIMEOUT_SECONDS", 1.0)

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main.app), base_url="http://test"
        ) as client:
            first = asyncio.create_task(client.post(
                "/parse", files={"file": ("sample.xml", SAMPLE.read_bytes())}
            ))
            for _ in range(100):
                if pid_file.exists():
                    break
                await asyncio.sleep(0.01)
            assert pid_file.exists()
            busy = await client.post(
                "/upload", files={"file": ("sample.xml", b"<POWERMART/>")}
            )
            assert busy.status_code == 429
            assert (await client.get("/health")).status_code == 200
            timed_out = await first
            assert timed_out.status_code == 504
            assert "10-second" in timed_out.json()["error"]
            recovered = await client.post(
                "/upload", files={"file": ("sample.xml", b"<POWERMART/>")}
            )
            assert recovered.status_code == 200

    asyncio.run(run())
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)
