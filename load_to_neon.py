"""
load_to_neon.py — step 6 (v2): write per-start rows to neon, now with opponent.

adds opp (text) and is_home (boolean). ALTER ... ADD COLUMN IF NOT EXISTS keeps
the existing table in sync without a rebuild. idempotent upsert as before.

    python load_to_neon.py
"""

import os
from pathlib import Path

import numpy as np
import pandas as pd
import psycopg2
from psycopg2.extras import execute_values

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

SRC = Path("data/agg/starts_rates_2026.parquet")
DB_URL = os.environ.get("PITCH_SHAPES_DB_URL") or os.environ.get("DATABASE_URL")

DDL = """
CREATE TABLE IF NOT EXISTS pitch_shapes (
    pitcher integer NOT NULL, pitch_type text NOT NULL,
    game_pk integer NOT NULL, stand text NOT NULL,
    game_date date, pitcher_name text, p_throws text,
    opp text, is_home boolean,
    velo real, ivb_in real, hb_in real, spin real, axis real,
    ext real, rel_x real, rel_z real,
    n_pitches integer, swings integer, whiffs integer, called integer,
    in_zone integer, ooz integer, chase_num integer, two_strike integer,
    pa integer, k integer, bbe integer, barrels integer, hardhits integer,
    edge_num integer, e_1b integer, e_2b integer, e_3b integer, e_hr integer,
    xwoba_num real, woba_num real, woba_den real,
    game_pitches integer, game_pitches_hand integer,
    updated_at timestamptz DEFAULT now(),
    PRIMARY KEY (pitcher, pitch_type, game_pk, stand)
);
ALTER TABLE pitch_shapes ADD COLUMN IF NOT EXISTS opp text;
ALTER TABLE pitch_shapes ADD COLUMN IF NOT EXISTS is_home boolean;
CREATE INDEX IF NOT EXISTS ix_pitch_shapes_pitcher ON pitch_shapes (pitcher);
CREATE INDEX IF NOT EXISTS ix_pitch_shapes_date ON pitch_shapes (game_date);
"""

STORE_COLS = [
    "pitcher", "pitch_type", "game_pk", "stand", "game_date", "pitcher_name", "p_throws",
    "opp", "is_home",
    "velo", "ivb_in", "hb_in", "spin", "axis", "ext", "rel_x", "rel_z",
    "n_pitches", "swings", "whiffs", "called", "in_zone", "ooz", "chase_num", "two_strike",
    "pa", "k", "bbe", "barrels", "hardhits", "edge_num", "e_1b", "e_2b", "e_3b", "e_hr",
    "xwoba_num", "woba_num", "woba_den", "game_pitches", "game_pitches_hand",
]
KEYS = ["pitcher", "pitch_type", "game_pk", "stand"]
INT_COLS = ["pitcher", "game_pk", "n_pitches", "swings", "whiffs", "called", "in_zone",
            "ooz", "chase_num", "two_strike", "pa", "k", "bbe", "barrels", "hardhits",
            "edge_num", "e_1b", "e_2b", "e_3b", "e_hr", "game_pitches", "game_pitches_hand"]


def py(v):
    if v is None:
        return None
    if isinstance(v, (bool, np.bool_)):
        return bool(v)
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(v, np.integer):
        return int(v)
    if isinstance(v, (float, np.floating)):
        return None if np.isnan(v) else float(v)
    return v


def main():
    if not DB_URL:
        raise SystemExit("set PITCH_SHAPES_DB_URL (or DATABASE_URL) to the write role")

    df = pd.read_parquet(SRC)
    df["game_date"] = pd.to_datetime(df["game_date"]).dt.date
    for c in INT_COLS:
        df[c] = df[c].fillna(0).astype(int)
    for c in ["pitch_type", "stand", "pitcher_name", "p_throws", "opp"]:
        if c in df.columns:
            df[c] = df[c].astype("object").where(df[c].notna(), None)

    records = [tuple(py(v) for v in row)
               for row in df[STORE_COLS].itertuples(index=False, name=None)]

    non_keys = [c for c in STORE_COLS if c not in KEYS]
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in non_keys)
    sql = (f"INSERT INTO pitch_shapes ({', '.join(STORE_COLS)}) VALUES %s "
           f"ON CONFLICT ({', '.join(KEYS)}) DO UPDATE SET {updates}, updated_at = now()")

    conn = psycopg2.connect(DB_URL)
    try:
        with conn.cursor() as cur:
            cur.execute(DDL)
            execute_values(cur, sql, records, page_size=1000)
        conn.commit()
    finally:
        conn.close()

    print(f"upserted {len(records)} rows into pitch_shapes")


if __name__ == "__main__":
    main()
