from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, File, UploadFile
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from api.parser import parse_powercenter_xml, topological_order
from api.parser.models import ParseResponse

MAX_FILE_SIZE = 5 * 1024 * 1024
FRONTEND_DIST = Path(__file__).resolve().parents[1] / "dist" / "public"
SAMPLES_DIR = Path(__file__).resolve().parents[3] / "samples"

app = FastAPI(title="Informatica to dbt Migrator")


@app.get("/health")
@app.get("/api/health")
@app.get("/healthz")
@app.get("/api/healthz")
async def health() -> dict[str, str]:
    return {"status": "ok"}


async def read_xml_upload(file: UploadFile) -> tuple[str, bytes] | JSONResponse:
    filename = file.filename or ""
    try:
        if not filename.lower().endswith(".xml"):
            return JSONResponse(status_code=400, content={"error": "Only .xml files are accepted."})
        contents = await file.read(MAX_FILE_SIZE + 1)
        if len(contents) > MAX_FILE_SIZE:
            return JSONResponse(status_code=413, content={"error": "File must be 5 MB or smaller."})
        return filename, contents
    finally:
        await file.close()


@app.post("/upload")
@app.post("/api/upload")
async def upload_xml(file: Annotated[UploadFile, File(...)]) -> JSONResponse:
    upload = await read_xml_upload(file)
    if isinstance(upload, JSONResponse):
        return upload
    filename, contents = upload
    return JSONResponse(content={"filename": filename, "size_bytes": len(contents)})


def parse_response(contents: bytes) -> JSONResponse:
    try:
        result = parse_powercenter_xml(contents)
        orders = {}
        for mapping in result.mappings:
            key = mapping.name
            if sum(item.name == key for item in result.mappings) > 1:
                key = f"{mapping.repository}/{mapping.folder}/{mapping.name}"
            if key in orders:
                raise ValueError(f"Duplicate mapping identity: {key}.")
            orders[key] = topological_order(mapping)
        response = ParseResponse(**result.model_dump(), execution_order=orders)
        return JSONResponse(content=response.model_dump())
    except ValueError as error:
        return JSONResponse(status_code=400, content={"error": str(error)})


@app.post("/parse")
@app.post("/api/parse")
async def parse_xml(file: Annotated[UploadFile, File(...)]) -> JSONResponse:
    upload = await read_xml_upload(file)
    if isinstance(upload, JSONResponse):
        return upload
    return parse_response(upload[1])


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
    return parse_response(contents)


if FRONTEND_DIST.is_dir():
    app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="frontend")
