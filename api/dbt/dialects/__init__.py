"""SQL dialect implementations used by the dbt generator."""

from . import duckdb, snowflake


_DIALECTS = {
    duckdb.name: duckdb,
    snowflake.name: snowflake,
}


def get_dialect(name: str = "snowflake"):
    try:
        return _DIALECTS[name.lower()]
    except KeyError as error:
        choices = ", ".join(sorted(_DIALECTS))
        raise ValueError(
            f"Unknown SQL dialect '{name}'. Choose one of: {choices}."
        ) from error
