"""Temporary, synthetic-data dbt validation using the DuckDB dialect."""

from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import duckdb

from api.parser.identifiers import normalize_identifier
from api.parser.models import Field, ParseResult, Source

from .generator import DbtProject, SeedTable, build_project


_SEED_ROWS = 6
_CLAIMS_MEMBER_SYNTHETIC_VALUES: dict[str, dict[str, tuple[str | None, ...]]] = {
    "claims": {
        "claim_id": ("C001", "C002", "C003", "C004", "C005", "C006"),
        "member_id": ("M001", "M002", "M002", "M004", "M999", None),
        "claim_amount": ("100", "200", "75", "300", "400", "500"),
    },
    "member": {
        "member_id": ("M001", "M002", "M003", "M004", "M005", None),
        "member_name": ("Ada", "Grace", "Katherine", "Alan", "Margaret", None),
    },
}
_CLAIMS_PROVIDER_LOOKUP_VALUES: dict[str, dict[str, tuple[str | None, ...]]] = {
    "claims": {
        "claim_id": ("C101", "C102", "C103", "C104", "C105", "C106"),
        "provider_id": ("P001", "P002", "P003", "P404", "P002", None),
        "claim_amount": ("110", "220", "330", "440", "550", "660"),
    },
    "provider": {
        "provider_id": ("P001", "P002", "P002", "P003", "P005", None),
        "provider_name": (
            "Alpha Clinic", "Provider First", "Provider Second",
            "Gamma Clinic", "Epsilon Clinic", None,
        ),
        "provider_tier": ("A", "B", "C", "A", "B", None),
    },
}


def _birthday(reference: date, years_ago: int = 25) -> date:
    try:
        return reference.replace(year=reference.year - years_ago)
    except ValueError:
        return reference.replace(year=reference.year - years_ago, day=28)


def _synthetic_value(field: Field, row_index: int, reference: date) -> str | None:
    name = normalize_identifier(field.name)
    datatype = normalize_identifier(field.datatype).replace("-", " ")

    if any(token in name for token in ("birth", "dob")):
        birthday = _birthday(reference)
        if row_index == 0:
            return birthday.isoformat()
        if row_index == 1:
            return (birthday + timedelta(days=1)).isoformat()
        return birthday.isoformat()
    if any(token in name for token in ("end", "expiry", "expiration")) and (
        "date" in name or "dt" in name or "time" in name
    ):
        values = [
            reference.isoformat(),
            reference.isoformat(),
            None,
            None,
            reference.isoformat(),
            (reference - timedelta(days=1)).isoformat(),
        ]
        return values[row_index]
    if any(token in name for token in ("date", "time", "start", "created", "updated")):
        return (reference - timedelta(days=365)).isoformat()
    if "status" in name:
        return None if row_index == 4 else "A"
    if "gender" in name or name.endswith("_sex"):
        return ("M", "F", "M", None, "F", "M")[row_index]
    if "first_name" in name or name.endswith("_fname"):
        return ("Ada", "Grace", None, None, "Hidden", "Filtered")[row_index]
    if "last_name" in name or name.endswith("_lname"):
        return ("Lovelace", "Hopper", "NullFirst", None, "Hidden", "Filtered")[row_index]
    if any(token in name for token in ("member_id", "customer_id", "account_id", "id")):
        return f"SYN-{row_index + 1:03}"
    if "plan" in name or "code" in name or name.endswith("_cd"):
        return f"P{row_index + 1}"
    if datatype in {"boolean", "bool"}:
        return ("TRUE", "FALSE", "TRUE", None, "FALSE", "TRUE")[row_index]
    if datatype in {"number", "numeric", "decimal", "integer", "int", "small integer", "bigint"}:
        return str(row_index + 1)
    if datatype in {"float", "double", "real"}:
        return str(row_index + 0.5)
    if datatype in {"binary", "varbinary"}:
        return "00"
    return f"synthetic_{row_index + 1}"


def synthetic_seed_rows(source: Source, reference: date | None = None) -> list[list[str | None]]:
    """Create repeatable representative rows, including NULLs and date boundaries."""
    reference = reference or datetime.now(timezone.utc).date()
    overrides = _CLAIMS_MEMBER_SYNTHETIC_VALUES.get(
        normalize_identifier(source.name), {}
    )
    overrides = {
        **overrides,
        **_CLAIMS_PROVIDER_LOOKUP_VALUES.get(
            normalize_identifier(source.name), {}
        ),
    }
    return [
        [
            overrides.get(normalize_identifier(field.name), ())[row_index]
            if normalize_identifier(field.name) in overrides
            else _synthetic_value(field, row_index, reference)
            for field in source.fields
        ]
        for row_index in range(_SEED_ROWS)
    ]


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _load_synthetic_sources(
    workspace: Path,
    seed_tables: list[SeedTable],
    reference: date,
) -> None:
    connection = duckdb.connect(str(workspace / "validation.duckdb"))
    try:
        for seed in seed_tables:
            fields_by_name: dict[str, tuple[int, Field]] = {}
            for index, field in enumerate(seed.source.fields):
                fields_by_name.setdefault(normalize_identifier(field.name), (index, field))
            selected = list(fields_by_name.values())
            table = f"main.{_quote_identifier(seed.identifier)}"
            if selected:
                columns = ", ".join(
                    f"{_quote_identifier(field.name)} VARCHAR"
                    for _, field in selected
                )
            else:
                columns = '"_validation_dummy" VARCHAR'
            connection.execute(f"CREATE TABLE {table} ({columns})")
            rows = synthetic_seed_rows(seed.source, reference)
            if not selected:
                values = [(None,) for _ in rows]
            else:
                values = [
                    tuple(row[index] for index, _field in selected)
                    for row in rows
                ]
            placeholders = ", ".join("?" for _ in range(len(values[0])))
            connection.executemany(
                f"INSERT INTO {table} VALUES ({placeholders})",
                values,
            )
    finally:
        connection.close()


def prepare_validation_workspace(
    result: ParseResult,
    workspace: Path,
    reference_date: date | None = None,
) -> DbtProject:
    """Write a DuckDB-specific project and synthetic seeds into an empty directory."""
    workspace.mkdir(parents=True, exist_ok=True)
    project = build_project(result, dialect="duckdb")
    profile = project.files.get("profiles.yml")
    if profile is not None:
        relative_path = "      path: validation.duckdb\n"
        if relative_path not in profile:
            raise ValueError("DuckDB validation profile is missing its database path.")
        project.files["profiles.yml"] = profile.replace(
            relative_path,
            f"      path: {json.dumps(str((workspace / 'validation.duckdb').resolve()))}\n",
            1,
        )
    for relative_path, content in project.files.items():
        destination = workspace / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")
    if reference_date is None:
        current = duckdb.connect(":memory:")
        try:
            reference_date = current.execute("SELECT CURRENT_DATE").fetchone()[0]
        finally:
            current.close()
    _load_synthetic_sources(workspace, project.seed_tables, reference_date)
    return project


def _run_dbt(runner: Any, workspace: Path, command: str) -> Any:
    return runner.invoke(
        [
            command,
            "--project-dir",
            str(workspace),
            "--profiles-dir",
            str(workspace),
            "--target-path",
            str(workspace / "target"),
            "--quiet",
        ],
    )


def _step(name: str, command: str | None = None) -> dict[str, str]:
    return {
        "name": name,
        "command": command or name,
        "status": "pending",
        "message": "",
    }


def _failure_text(result: Any) -> str:
    error = getattr(result, "exception", None)
    if error is not None:
        return str(error)[-3000:]
    return "dbt command failed. Check the per-model results and retry."


def _model_descriptors(project: DbtProject) -> list[dict[str, Any]]:
    blocked_paths = {item["path"]: item for item in project.blocked_models}
    models = []
    for path in sorted(
        item for item in project.files
        if item.startswith("models/") and item.endswith(".sql")
    ):
        name = Path(path).stem
        blocked = blocked_paths.get(path)
        models.append({
            "name": name,
            "kind": "staging" if "/staging/" in path else "target",
            "path": path,
            "status": "blocked" if blocked else "pending",
            "phase": "generation" if blocked else "",
            "message": (
                "Generation blocked: " + ", ".join(blocked["reasons"])
                if blocked else ""
            ),
            "row_count": None,
        })
    return models


def execute_validation_workspace(
    workspace: Path,
    project: DbtProject,
) -> dict[str, Any]:
    """Seed, compile, and run models, reporting each model's final status."""
    os.environ.setdefault("DBT_SEND_ANONYMOUS_USAGE_STATS", "false")
    api_package_directory = Path(__file__).resolve().parents[1]
    sys.path[:] = [
        entry for entry in sys.path
        if Path(entry or os.curdir).resolve() != api_package_directory
    ]
    from dbt.cli.main import dbtRunner

    runner = dbtRunner()
    steps = [
        {
            **_step("seed"),
            "status": "passed",
            "message": "Synthetic source rows loaded into DuckDB.",
        },
        _step("compile"),
        _step("run"),
    ]
    models = _model_descriptors(project)
    active_models = [model for model in models if model["status"] != "blocked"]

    for step in steps[1:]:
        previous_cwd = Path.cwd()
        try:
            os.chdir(workspace)
            completed = _run_dbt(runner, workspace, step["command"])
        except Exception as error:
            step["status"] = "failed"
            step["message"] = str(error)
            _fail_pending_models(active_models, step["name"], step["message"])
            break
        finally:
            os.chdir(previous_cwd)
        if not completed.success:
            step["status"] = "failed"
            step["message"] = _failure_text(completed)
            if step["name"] == "run":
                _apply_run_results(workspace, active_models)
            _fail_pending_models(active_models, step["name"], step["message"])
            break
        step["status"] = "passed"
        step["message"] = {
            "seed": "Synthetic source rows loaded.",
            "compile": "dbt compiled the generated models.",
            "run": "dbt executed the generated models.",
        }[step["name"]]
        if step["name"] == "run":
            _apply_run_results(workspace, active_models)

    status = (
        "failed" if any(model["status"] == "failed" for model in models)
        else "blocked" if any(model["status"] == "blocked" for model in models)
        else "passed"
    )
    return {
        "status": status,
        "dialect": "duckdb",
        "steps": steps,
        "models": models,
    }


def _fail_pending_models(
    models: list[dict[str, Any]],
    phase: str,
    message: str,
) -> None:
    for model in models:
        if model["status"] == "pending":
            model["status"] = "failed"
            model["phase"] = phase
            model["message"] = message


def _apply_run_results(
    workspace: Path,
    models: list[dict[str, Any]],
) -> None:
    results_path = workspace / "target" / "run_results.json"
    if not results_path.is_file():
        _fail_pending_models(models, "run", "dbt did not produce run_results.json.")
        return
    results = json.loads(results_path.read_text(encoding="utf-8"))
    by_name = {
        item.get("unique_id", "").rsplit(".", 1)[-1]: item
        for item in results.get("results", [])
    }
    database = workspace / "validation.duckdb"
    connection = duckdb.connect(str(database))
    try:
        for model in models:
            result = by_name.get(model["name"])
            if result is None:
                model.update(
                    status="failed",
                    phase="run",
                    message="dbt returned no result for this model.",
                )
                continue
            result_status = result.get("status", "")
            if result_status == "success":
                model["status"] = "passed"
                model["phase"] = "run"
                model["message"] = result.get("message") or "Model executed."
                quoted_name = '"' + model["name"].replace('"', '""') + '"'
                model["row_count"] = connection.execute(
                    f"SELECT COUNT(*) FROM main.{quoted_name}"
                ).fetchone()[0]
            else:
                model["status"] = "failed"
                model["phase"] = "run"
                model["message"] = result.get("message") or f"dbt status: {result_status}."
    finally:
        connection.close()


def validate_project(result: ParseResult) -> dict[str, Any]:
    """Run the complete local validation pipeline and clean temporary files."""
    with TemporaryDirectory(prefix="infa2dbt-validate-") as directory:
        workspace = Path(directory)
        project = prepare_validation_workspace(result, workspace)
        return execute_validation_workspace(workspace, project)
