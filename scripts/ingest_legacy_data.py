"""Load the raw logistics CSV into the legacy-style table dbo.TBL_SC_FLEET_HIST_RAW.

Usage:
    python scripts/ingest_legacy_data.py --target local --dry-run   # show plan, write nothing
    python scripts/ingest_legacy_data.py --target local             # create table + load
    python scripts/ingest_legacy_data.py --target aws               # needs the SSH tunnel open

Safety:
  * The table is NEVER dropped or replaced.
  * If the table already exists the script stops without writing, unless
    --append is given (which adds rows on top of the existing ones).
  * The whole load runs in one transaction: on any error nothing is committed.
"""
from __future__ import annotations

import argparse
import sys

import pandas as pd
from sqlalchemy import inspect, text

from db_config import PROJECT_ROOT, TARGETS, ConfigError, get_engine, get_target, tunnel_hint

DATA_PATH = PROJECT_ROOT / "data" / "raw" / "dynamic_supply_chain_logistics_dataset.csv"
TABLE_NAME = "TBL_SC_FLEET_HIST_RAW"
SCHEMA = "dbo"

# Map clean columns to a messy 2000s legacy enterprise schema
LEGACY_MAPPING = {
    "timestamp": "TS_UTC",
    "vehicle_gps_latitude": "V_LAT",
    "vehicle_gps_longitude": "V_LON",
    "iot_temperature": "IOT_TEMP_VAL_C",
    "cargo_condition_status": "CGO_COND_CD",
    "risk_classification": "RISK_CLS_TXT",
    "delay_probability": "DELAY_PROB_DEC",
    "port_congestion_level": "PRT_CNG_LVL",
    "route_risk_level": "RT_RSK_IDX",
}


def build_legacy_frame() -> pd.DataFrame:
    print(f"Loading CSV from {DATA_PATH}...")
    df = pd.read_csv(DATA_PATH)
    df_legacy = df[list(LEGACY_MAPPING.keys())].rename(columns=LEGACY_MAPPING)
    # Fake ingestion flag to make it look like an automated legacy system
    df_legacy["SYS_INGEST_FLAG"] = "Y"
    print(f"Prepared {len(df_legacy)} rows x {len(df_legacy.columns)} columns")
    return df_legacy


def count_rows(conn) -> int:
    return conn.execute(text(f"SELECT COUNT(*) FROM {SCHEMA}.{TABLE_NAME}")).scalar_one()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--target", choices=TARGETS, help="local or aws (default: DB_TARGET from .env)")
    parser.add_argument("--append", action="store_true",
                        help="add rows to an existing table (may create duplicates)")
    parser.add_argument("--dry-run", action="store_true", help="connect and report, but write nothing")
    args = parser.parse_args()

    try:
        cfg = get_target(args.target)
    except ConfigError as exc:
        print(f"ERROR: {exc}")
        return 2

    print(f"Target: {cfg.describe()}")
    if not cfg.port_is_open():
        print(f"ERROR: nothing is listening on {cfg.host}:{cfg.port}.")
        if cfg.name == "aws":
            print(f"Open the SSH tunnel first:\n  {tunnel_hint()}")
        else:
            print("Start the container first: docker start legacy-mssql")
        return 2

    df_legacy = build_legacy_frame()
    engine = get_engine(cfg, fast_executemany=True)

    with engine.connect() as conn:
        server = conn.execute(text("SELECT @@SERVERNAME")).scalar_one()
        current_db = conn.execute(text("SELECT DB_NAME()")).scalar_one()
        print(f"Connected to server '{server}', database '{current_db}'")
        if current_db != cfg.database:
            print(f"ERROR: expected database '{cfg.database}' but connected to '{current_db}'.")
            return 2
        exists = inspect(conn).has_table(TABLE_NAME, schema=SCHEMA)
        before = count_rows(conn) if exists else 0

    if exists and not args.append:
        print(f"{SCHEMA}.{TABLE_NAME} already exists with {before} rows. Nothing written.")
        print("Existing data is preserved. Use --append only if you really want to add rows on top.")
        return 0

    action = "append to" if exists else "create"
    if args.dry_run:
        print(f"[dry-run] Would {action} {SCHEMA}.{TABLE_NAME} "
              f"({before} existing rows + {len(df_legacy)} new). Nothing written.")
        return 0

    print(f"Ingesting into {SCHEMA}.{TABLE_NAME} ({action}). This may take a minute...")
    with engine.begin() as conn:  # single transaction: commit on success, rollback on error
        df_legacy.to_sql(
            TABLE_NAME, conn, schema=SCHEMA, index=False,
            if_exists="append" if exists else "fail",  # never "replace"
            chunksize=1000,
        )
        after = count_rows(conn)

    print(f"Done. {SCHEMA}.{TABLE_NAME}: {before} rows before, {after} rows after "
          f"on {cfg.name.upper()} ({cfg.host}:{cfg.port}/{cfg.database}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
