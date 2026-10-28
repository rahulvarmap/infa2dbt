from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, File, UploadFile
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

MAX_FILE_SIZE = 5 * 1024 * 1024
FRONTEND_DIST = Path(__file__).resolve().parents[1] / "dist" / "public"

app = FastAPI(title="Informatica to dbt Migrator")


@app.get("/health")
@app.get("/api/health")
@app.get("/healthz")
@app.get("/api/healthz")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/upload")
@app.post("/api/upload")
async def upload_xml(file: Annotated[UploadFile, File(...)]) -> JSONResponse:
    filename = file.filename or ""
    if not filename.lower().endswith(".xml"):
        await file.close()
        return JSONResponse(
            status_code=400,
            content={"error": "Only .xml files are accepted."},
        )

    try:
        contents = await file.read(MAX_FILE_SIZE + 1)
        if len(contents) > MAX_FILE_SIZE:
            return JSONResponse(
                status_code=413,
                content={"error": "File must be 5 MB or smaller."},
            )
        return JSONResponse(
            content={"filename": filename, "size_bytes": len(contents)}
        )
    finally:
        await file.close()


if FRONTEND_DIST.is_dir():
    app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="frontend")
