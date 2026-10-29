import asyncio
import importlib
import json
import sys
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.datastructures import FormData, UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.staticfiles import StaticFiles

from api.request_limits import (
    MAX_FILE_SIZE,
    MAX_MULTIPART_FIELDS,
    MAX_MULTIPART_FIELD_SIZE,
    ProcessingLimitsMiddleware,
)

FRONTEND_DIST = Path(__file__).resolve().parents[1] / "dist" / "public"
SAMPLES_DIR = Path(__file__).resolve().parents[3] / "samples"
REPO_ROOT = Path(__file__).resolve().parents[3]
WORKER_SCRIPT = REPO_ROOT / "api" / "processing_worker.py"

app = FastAPI(title="Informatica to dbt Migrator")
app.add_middleware(ProcessingLimitsMiddleware)


@app.get("/health")
@app.get("/api/health")
@app.get("/healthz")
@app.get("/api/healthz")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/ready")
@app.get("/api/ready")
async def readiness() -> JSONResponse:
    try:
        importlib.import_module("api.parser")
        importlib.import_module("api.parser.lineage")
    except ImportError:
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready", "checks": {"dependencies": "unavailable"}},
        )
    sample = sample_files().get("m_member_enrollment.xml")
    if sample is None or sample.stat().st_size > MAX_FILE_SIZE:
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready", "checks": {"sample": "unavailable"}},
        )
    return JSONResponse(content={"status": "ready"})


async def read_xml_upload(
    request: Request,
    *,
    allowed_fields: set[str] | None = None,
    require_file: bool = True,
) -> tuple[str | None, bytes | None, dict[str, str]] | JSONResponse:
    allowed_fields = allowed_fields or set()
    try:
        form: FormData = await request.form(
            max_files=1,
            max_fields=MAX_MULTIPART_FIELDS,
            max_part_size=MAX_MULTIPART_FIELD_SIZE,
        )
    except StarletteHTTPException as error:
        detail = str(error.detail)
        too_many_parts = "Too many files" in detail or "Too many fields" in detail
        message = (
            "Include at most one uploaded file and four text fields."
            if too_many_parts
            else "Upload a valid multipart form."
        )
        return JSONResponse(status_code=413 if too_many_parts else 400, content={"error": message})

    fields: dict[str, str] = {}
    filename: str | None = None
    contents: bytes | None = None
    try:
        for key, value in form.multi_items():
            if isinstance(value, UploadFile):
                if key != "file":
                    return JSONResponse(
                        status_code=400,
                        content={"error": "Use the multipart field named 'file'."},
                    )
                filename = value.filename or ""
                if not filename.lower().endswith(".xml"):
                    return JSONResponse(
                        status_code=400,
                        content={"error": "Only .xml files are accepted."},
                    )
                contents = await value.read(MAX_FILE_SIZE + 1)
                if len(contents) > MAX_FILE_SIZE:
                    return JSONResponse(
                        status_code=413,
                        content={"error": "File must be 5 MB or smaller."},
                    )
                continue
            if key == "file" or key not in allowed_fields:
                return JSONResponse(
                    status_code=400,
                    content={"error": f"Unexpected multipart field: {key}."},
                )
            if key in fields:
                return JSONResponse(
                    status_code=400,
                    content={"error": f"Duplicate multipart field: {key}."},
                )
            fields[key] = value

        if require_file and filename is None:
            return JSONResponse(
                status_code=422,
                content={"error": "Provide one XML file in the multipart field named 'file'."},
            )
        return filename, contents, fields
    finally:
        await form.close()


@app.post("/upload")
@app.post("/api/upload")
async def upload_xml(request: Request) -> JSONResponse:
    upload = await read_xml_upload(request)
    if isinstance(upload, JSONResponse):
        return upload
    filename, contents, _ = upload
    assert filename is not None and contents is not None
    return JSONResponse(content={"filename": filename, "size_bytes": len(contents)})


async def process_in_worker(
    contents: bytes, fields: dict[str, str] | None = None
) -> JSONResponse:
    worker_fields = dict(fields or {})
    mode = worker_fields.pop("mode", "parse")
    header = json.dumps({"mode": mode, "fields": worker_fields}).encode() + b"\n"
    process: asyncio.subprocess.Process | None = None
    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(WORKER_SCRIPT),
            cwd=REPO_ROOT,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await process.communicate(header + contents)
        if process.returncode != 0:
            return JSONResponse(
                status_code=500, content={"error": "XML processing failed."}
            )
        result: Any = json.loads(stdout)
        if (
            not isinstance(result, dict)
            or not isinstance(result.get("status_code"), int)
            or not isinstance(result.get("content"), dict)
        ):
            raise ValueError("Invalid response from XML worker.")
        return JSONResponse(
            status_code=result["status_code"],
            content=result["content"],
        )
    except asyncio.CancelledError:
        if process is not None and process.returncode is None:
            process.kill()
            await process.communicate()
        raise
    except (OSError, ValueError, json.JSONDecodeError):
        return JSONResponse(status_code=500, content={"error": "XML processing failed."})


@app.post("/parse")
@app.post("/api/parse")
async def parse_xml(request: Request) -> JSONResponse:
    upload = await read_xml_upload(request)
    if isinstance(upload, JSONResponse):
        return upload
    _, contents, _ = upload
    assert contents is not None
    return await process_in_worker(contents)


def sample_files() -> dict[str, Path]:
    if not SAMPLES_DIR.is_dir():
        return {}
    return {
        path.name: path for path in sorted(SAMPLES_DIR.iterdir())
        if path.is_file() and not path.is_symlink() and path.suffix.lower() == ".xml"
    }


@app.get("/samples")
@app.get("/api/samples")
async def list_samples() -> list[dict]:
    return [{"filename": name, "size_bytes": path.stat().st_size} for name, path in sample_files().items()]


@app.post("/samples/{filename}/parse")
@app.post("/api/samples/{filename}/parse")
async def parse_sample(filename: str) -> JSONResponse:
    path = sample_files().get(filename)
    if path is None:
        return JSONResponse(status_code=404, content={"error": "Sample not found."})
    with path.open("rb") as sample:
        contents = sample.read(MAX_FILE_SIZE + 1)
    if len(contents) > MAX_FILE_SIZE:
        return JSONResponse(status_code=413, content={"error": "Sample must be 5 MB or smaller."})
    return await process_in_worker(contents)


@app.post("/lineage/trace")
@app.post("/api/lineage/trace")
async def trace_lineage(request: Request) -> JSONResponse:
    upload = await read_xml_upload(
        request,
        allowed_fields={"target_instance", "target_field", "mapping", "sample_name"},
        require_file=False,
    )
    if isinstance(upload, JSONResponse):
        return upload
    _, file_contents, fields = upload
    if "target_instance" not in fields or "target_field" not in fields:
        return JSONResponse(
            status_code=422,
            content={"error": "Provide target_instance and target_field."},
        )
    sample_name = fields.get("sample_name")
    if (file_contents is None) == (sample_name is None):
        return JSONResponse(
            status_code=400,
            content={"error": "Provide exactly one XML file or sample_name."},
        )
    if file_contents is not None:
        contents = file_contents
    else:
        assert sample_name is not None
        path = sample_files().get(sample_name)
        if path is None:
            return JSONResponse(status_code=404, content={"error": "Sample not found."})
        with path.open("rb") as sample:
            contents = sample.read(MAX_FILE_SIZE + 1)
        if len(contents) > MAX_FILE_SIZE:
            return JSONResponse(status_code=413, content={"error": "Sample must be 5 MB or smaller."})
    worker_fields = {
        "mode": "trace",
        "target_instance": fields["target_instance"],
        "target_field": fields["target_field"],
    }
    if "mapping" in fields:
        worker_fields["mapping"] = fields["mapping"]
    return await process_in_worker(contents, worker_fields)


if FRONTEND_DIST.is_dir():
    app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="frontend")
