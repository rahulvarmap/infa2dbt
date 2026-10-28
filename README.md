# infa2dbt

Informatica → dbt Migrator accepts one `.xml` file at a time. Uploads are
validated in memory; file contents are not saved or logged.

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
pnpm --filter @workspace/infa2dbt run dev
```

The FastAPI routes are `GET /health` and `POST /upload`; when accessed through
the app's `/api` service, use `/api/health` and `/api/upload`.

## Build and run the production app

```sh
pnpm --filter @workspace/infa2dbt run build
PORT=8000 python artifacts/infa2dbt/api/serve.py
```

After the frontend build, FastAPI serves the static app from
`artifacts/infa2dbt/dist/public` as well as the API routes.
