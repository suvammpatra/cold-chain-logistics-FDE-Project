"""Read-only connectivity check for the local and/or AWS SQL Server targets.

Usage:
    python scripts/test_db_connection.py --target local
    python scripts/test_db_connection.py --target aws      # SSH tunnel must be open
    python scripts/test_db_connection.py --target all

Only SELECT statements are executed. Nothing is created, changed or deleted.
"""
from __future__ import annotations

import argparse
import sys

import pyodbc

from db_config import TARGETS, ConfigError, get_target, tunnel_hint

TABLE = "TBL_SC_FLEET_HIST_RAW"


def check(target_name: str) -> bool:
    print(f"\n=== {target_name.upper()} ===")
    try:
        cfg = get_target(target_name)
    except ConfigError as exc:
        print(f"FAIL  config: {exc}")
        return False
    print(f"Target   : {cfg.describe()}")

    if not cfg.port_is_open():
        print(f"FAIL  nothing is listening on {cfg.host}:{cfg.port}")
        if target_name == "aws":
            print("      The SSH tunnel is not running. Open it in a separate terminal:")
            print(f"      {tunnel_hint()}")
        else:
            print("      Start the container: docker start legacy-mssql")
        return False

    try:
        conn = pyodbc.connect(cfg.odbc_string(database="master"), timeout=15, readonly=True)
    except pyodbc.Error as exc:
        print(f"FAIL  port is open but SQL login failed: {exc.args[-1]}")
        return False

    cur = conn.cursor()
    name, version, edition = cur.execute(
        "SELECT @@SERVERNAME, CAST(SERVERPROPERTY('ProductVersion') AS nvarchar(50)), "
        "CAST(SERVERPROPERTY('Edition') AS nvarchar(100))"
    ).fetchone()
    cpus, mem_mb = cur.execute(
        "SELECT cpu_count, physical_memory_kb / 1024 FROM sys.dm_os_sys_info"
    ).fetchone()
    print(f"Server   : {name}  |  SQL {version} {edition}  |  {cpus} CPU, {mem_mb} MB RAM")
    print(f"Login    : {cur.execute('SELECT SUSER_SNAME()').fetchone()[0]}")

    dbs = [r[0] for r in cur.execute(
        "SELECT name FROM sys.databases WHERE state_desc = 'ONLINE' ORDER BY database_id"
    ).fetchall()]
    print(f"Databases: {', '.join(dbs)}")

    ok = True
    if cfg.database not in dbs:
        print(f"FAIL  configured database '{cfg.database}' does not exist on this server")
        ok = False

    found = False
    for db in dbs:
        if db in ("tempdb", "model", "msdb"):
            continue
        if cur.execute(f"SELECT OBJECT_ID(N'[{db}].dbo.{TABLE}', N'U')").fetchone()[0] is None:
            continue
        found = True
        total = cur.execute(f"SELECT COUNT(*) AS total_rows FROM [{db}].dbo.{TABLE}").fetchone()[0]
        marker = "  <- configured database" if db == cfg.database else ""
        print(f"Table    : [{db}].dbo.{TABLE} total_rows={total}{marker}")
    if not found:
        print(f"Table    : dbo.{TABLE} not found in any user database (not ingested yet)")

    conn.close()
    print("PASS  connected" if ok else "FAIL")
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--target", choices=(*TARGETS, "all"), default="all")
    args = parser.parse_args()
    targets = TARGETS if args.target == "all" else (args.target,)
    results = {t: check(t) for t in targets}
    print("\nSummary: " + ", ".join(f"{t}={'PASS' if r else 'FAIL'}" for t, r in results.items()))
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
