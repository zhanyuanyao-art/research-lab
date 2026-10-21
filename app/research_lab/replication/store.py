"""DuckDB store for datasets: one connection helper, table naming, parquet copies and file paths.

SQLite keeps the dataset registry; DuckDB (`<data>/lab.duckdb`) holds the columns. Connections are
short-lived so the CLI and the web process can take turns on the file lock."""
from __future__ import annotations

import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import duckdb
import pandas as pd

from ..config import get_paths

_lock = threading.RLock()


def duck_path() -> Path:
    return get_paths().data / "lab.duckdb"


def datasets_root() -> Path:
    return get_paths().data / "datasets"


def dataset_dir(ds_id: str) -> Path:
    return datasets_root() / ds_id


def parquet_path(ds_id: str) -> Path:
    return dataset_dir(ds_id) / "data.parquet"


def figures_dir() -> Path:
    d = get_paths().data / "figures"
    d.mkdir(parents=True, exist_ok=True)
    return d


@contextmanager
def connect(read_only: bool = False) -> Iterator[duckdb.DuckDBPyConnection]:
    """The one way into lab.duckdb. Serialised within the process; closed on exit."""
    path = duck_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        con = duckdb.connect(str(path), read_only=read_only and path.exists())
        try:
            yield con
        finally:
            con.close()


def table_name(ds_id: str) -> str:
    """DS-2026-0001 → ds_2026_0001."""
    parts = ds_id.split("-")
    if len(parts) != 3 or parts[0] != "DS" or not (parts[1].isdigit() and parts[2].isdigit()):
        raise ValueError(f"not a dataset id: {ds_id!r}")
    return f"ds_{parts[1]}_{parts[2]}"


def qi(name: str) -> str:
    """Quote an identifier for DuckDB (column names may be Chinese or contain spaces)."""
    return '"' + str(name).replace('"', '""') + '"'


def sql_str(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def write_df(ds_id: str, df: pd.DataFrame) -> None:
    """Create or replace the dataset table and refresh its parquet copy."""
    table = table_name(ds_id)
    pq = parquet_path(ds_id)
    pq.parent.mkdir(parents=True, exist_ok=True)
    with connect() as con:
        con.register("_incoming_df", df)
        try:
            con.execute(f"CREATE OR REPLACE TABLE {table} AS SELECT * FROM _incoming_df")
        finally:
            con.unregister("_incoming_df")
        con.execute(f"COPY {table} TO {sql_str(str(pq))} (FORMAT PARQUET)")


def execute_replace(ds_id: str, select_sql: str) -> None:
    """Replace the table with `select_sql` (which reads FROM the table) and refresh the parquet copy."""
    table = table_name(ds_id)
    with connect() as con:
        con.execute(f"CREATE OR REPLACE TABLE {table} AS {select_sql}")
        con.execute(f"COPY {table} TO {sql_str(str(parquet_path(ds_id)))} (FORMAT PARQUET)")


def read_df(ds_id: str, columns: list[str] | None = None, limit: int | None = None) -> pd.DataFrame:
    table = table_name(ds_id)
    cols = ", ".join(qi(c) for c in columns) if columns else "*"
    sql = f"SELECT {cols} FROM {table}" + (f" LIMIT {int(limit)}" if limit else "")
    with connect(read_only=True) as con:
        return con.execute(sql).df()


def describe(ds_id: str) -> list[tuple[str, str]]:
    with connect(read_only=True) as con:
        return [(r[0], r[1]) for r in con.execute(f"DESCRIBE {table_name(ds_id)}").fetchall()]


def exists(ds_id: str) -> bool:
    with connect(read_only=True) as con:
        row = con.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_name = ?", [table_name(ds_id)]).fetchone()
    return bool(row and row[0])


def drop(ds_id: str) -> None:
    with connect() as con:
        con.execute(f"DROP TABLE IF EXISTS {table_name(ds_id)}")


def read_parquet_file(path: Path) -> pd.DataFrame:
    """Parquet via DuckDB, so pandas needs no pyarrow."""
    con = duckdb.connect(":memory:")
    try:
        return con.execute(f"SELECT * FROM read_parquet({sql_str(str(path))})").df()
    finally:
        con.close()
