# infa2dbt

Informatica → dbt Migrator parses one PowerCenter `.xml` export at a time into
sources, targets, mappings, transformations, ports, instances, and connectors.
Review the Summary, Ports, and collapsible Raw JSON tabs, or try a bundled sample.
Uploads are processed in memory; their contents are not saved or logged.
There is no LLM, database, authentication, or dbt code generation in Phase 1.

## Requirements

- Python 3.11
- Node.js and pnpm

## Run in development

Install the frontend dependencies with `pnpm install`. Install the API
dependencies with:

```sh
python3.11 -m pip install -r artifacts/infa2dbt/api/requirements.txt
```

Start the FastAPI service and Vite frontend in separate terminals:

```sh
PORT=8080 python artifacts/infa2dbt/api/serve.py
```

```sh
PORT=24518 BASE_PATH=/ pnpm --filter @workspace/infa2dbt run dev
```

The FastAPI routes below are also available with an `/api` prefix in Replit:

- `GET /health`: API readiness.
- `POST /upload`: multipart `file`, `.xml` only, at most 5 MiB; returns filename and byte count.
- `POST /parse`: the same multipart upload; returns the intermediate representation
  (`sources`, `targets`, `mappings`) and `execution_order`, keyed by mapping name.
  Duplicate mapping names are keyed by `repository/folder/name`.
- `GET /samples`: filename and size for each XML file in the root `samples/` directory.
- `POST /samples/{filename}/parse`: parse a bundled sample without uploading it.

`api/parser/` contains the standalone Pydantic models, lxml parser, and stable
topological ordering. Field precision and scale remain strings to preserve XML
values without lossy coercion. DTD loading, network access, and entity resolution
are disabled. Entity declarations/references, malformed XML, unknown connector
instances, and cycles produce clear 400 errors. Unknown transformation types are
preserved. The existing FastAPI entrypoint stays in `artifacts/infa2dbt/api/`.

```sh
curl -F file=@samples/m_member_enrollment.xml http://localhost:8080/parse
```

## Tests

Install the root Python dependencies with `uv sync` (including `pytest` and the
HTTP test client), then run from the project root:

```sh
python -m pytest api/tests -q
```

The sample must contain 1 source, 1 target, 3 transformations, 5 instances,
and 35 connectors, ordered as:
`MEMBER_ENROLLMENT → SQ_MEMBER_ENROLLMENT → EXP_MEMBER_DERIVE → FIL_ACTIVE_MEMBERS → DIM_MEMBER`.
Additional tests cover mixed-case tags/attributes, unknown/reusable transformations,
multiple folders/mappings, unsafe entities, cycles, upload limits, and sample routes.

## Build and run the production app

```sh
PORT=24518 BASE_PATH=/ pnpm --filter @workspace/infa2dbt run build
PORT=8000 python artifacts/infa2dbt/api/serve.py
```

After the frontend build, FastAPI serves the static app from
`artifacts/infa2dbt/dist/public` as well as the API routes.
