# infa2dbt

An internal PowerCenter XML inspector with mapping graphs and column tracing.
dbt model generation is planned but not implemented. Use synthetic/sample exports
only. No application database or authentication is used.

## Run and validate

- Python 3.11 dependencies: `artifacts/infa2dbt/api/requirements.txt` is the single
  pinned runtime/test dependency source, including NetworkX and transitive packages.
- `bash scripts/install-infa2dbt-python-deps.sh` installs those pins into `.pythonlibs`.
- The installer uses uv and `security-overrides.txt` to override the Snowflake
  connector's stale `filelock<4` cap with the security-fixed release. The root uv
  configuration and exported dbt projects carry the same exception.
- `PORT=8080 bash scripts/run-infa2dbt-api.sh` starts FastAPI.
- `PORT=24518 BASE_PATH=/ pnpm --filter @workspace/infa2dbt run dev` starts Vite.
- `python -m pytest api/tests -q` runs backend/parser/lineage tests.
- `pnpm --filter @workspace/infa2dbt test` runs frontend tests.
- `pnpm run typecheck` checks the TypeScript workspace.
- `bash scripts/build-infa2dbt-web.sh` builds the production frontend and installs Python dependencies.
- `pnpm --filter @workspace/api-spec run codegen` regenerates clients and schemas.

## Architecture

- React/Vite/Tailwind frontend: `artifacts/infa2dbt/src/`.
- FastAPI routes and static serving: `artifacts/infa2dbt/api/main.py`.
- XML parser, models, ordering, and lineage: `api/parser/`.
- Subprocess orchestration isolates parse/trace work from the API event loop.
- OpenAPI contract: `lib/api-spec/openapi.yaml`; generated clients/schemas live in `lib/api-client-react` and `lib/api-zod`.
- Bundled samples: `samples/`.
- Production commands belong to each artifact's `.replit-artifact/artifact.toml`.
  FastAPI serves built frontend files from `artifacts/infa2dbt/dist/public`.
- The post-merge hook installs locked Node/Python dependencies. It never pushes a DB schema.
  Unused Node/DB scaffolding remains but is not the running backend.

## Operational limits

- `/health` and `/healthz` are liveness probes; `/ready` is a separate dependency/sample readiness probe.
  These routes also have `/api` aliases. Production API startup uses `/api/ready`.
- Complete request bodies are bounded before multipart parsing; only one uploaded
  file of at most 5 MiB is accepted, with 128 KiB allowed for multipart overhead.
- Parse/trace work accepts at most 50 mappings. Processing is terminated after 10
  seconds, excluding client body transfer time.
- An OS file lock rejects overlapping upload/parse/trace requests with `429` across
  processes on the same machine. Set Publishing **Max machines = 1** to maintain
  that limit across the deployment; locks are not distributed across machines.
- Multipart files may temporarily spool to disk and are closed after request handling.
  Do not log XML contents. No durable upload storage is introduced.

## Scope and change discipline

See `PLAN.md` for approved product decisions and remaining production work.
Do not modify parser or lineage semantics as part of deployment/resource-limit work.
