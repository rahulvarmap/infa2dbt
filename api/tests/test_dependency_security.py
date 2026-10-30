"""Regression coverage for the security override shared by installs and exports."""

from pathlib import Path
import tomllib

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from api.dbt.dependency_lock import DBT_REQUIREMENTS_LOCK, DBT_SECURITY_OVERRIDES


ROOT = Path(__file__).resolve().parents[2]


def _pins(text: str) -> dict[str, Requirement]:
    pins = {}
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            req = Requirement(line)
            pins[canonicalize_name(req.name)] = req
    return pins


def test_security_pins_agree_across_runtime_project_and_export():
    runtime = _pins((ROOT / "artifacts/infa2dbt/api/requirements.txt").read_text())
    exported = _pins(DBT_REQUIREMENTS_LOCK)
    override = _pins((ROOT / "artifacts/infa2dbt/api/security-overrides.txt").read_text())
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    locked = {
        package["name"]: package["version"]
        for package in tomllib.loads((ROOT / "uv.lock").read_text())["package"]
    }
    assert set(override) == {"filelock"}
    assert _pins(DBT_SECURITY_OVERRIDES) == override
    assert _pins("\n".join(project["tool"]["uv"]["override-dependencies"])) == override
    for name in ("filelock", "certifi", "dbt-snowflake"):
        assert runtime[name].specifier == exported[name].specifier
        assert locked[name] in runtime[name].specifier
    assert "4.2.4" in override["filelock"].specifier


def test_snowflake_cache_persists_with_security_fixed_filelock(tmp_path):
    from snowflake.connector.cache import SFDictFileCache

    cache_path = str(tmp_path / "connector-cache")
    cache = SFDictFileCache(cache_path, load_if_file_exists=False)
    cache["probe"] = "security-update"
    assert cache._save(force_flush=True)
    reloaded = SFDictFileCache(cache_path)
    assert reloaded["probe"] == "security-update"


def test_snowflake_crl_lock_retains_mutual_exclusion(tmp_path):
    import pytest
    from filelock import Timeout
    from snowflake.connector.crl_cache import CRLFileCache

    cache = CRLFileCache(cache_dir=tmp_path)
    path = tmp_path / "certificate.crl"
    holder = cache._get_crl_file_lock(path)
    contender = cache._get_crl_file_lock(path)
    with holder:
        assert holder.is_locked
        with pytest.raises(Timeout):
            contender.acquire(timeout=0)
    with contender:
        assert contender.is_locked
