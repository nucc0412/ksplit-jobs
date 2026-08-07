"""
compute_rates.py — step 5: counts -> rates.

reads the per-start counts (step 4) and turns them into the rates the tracker
shows. per-start rates are for the chart dots. all the count columns are kept,
so the trends view (step 7) can still recompute windowed rates from counts.

the baseline is deliberately NOT here. it lives in the view as a window over
the counts, so it recomputes from counts on read, never from an average of
these per-start rates.

edge% needs pitch-level geometry, so this reads the clean frame once to add an
edge count. it approximates savant's shadow-zone edge, it is not exact.

    python compute_rates.py
reads:  data/agg/starts_2026.parquet, data/clean/pitches_2026.parquet
writes: data/agg/starts_rates_2026.parquet
"""

from pathlib import Path

import numpy as np
import pandas as pd

AGG = Path("data/agg/starts_2026.parquet")
CLEAN = Path("data/clean/pitches_2026.parquet")
OUT = Path("data/agg/starts_rates_2026.parquet")

PLATE_HALF = 0.83     # ft, outer edge of the rulebook zone (plate half + ball)
EDGE_BAND = 0.20      # ft, band straddling the zone boundary counted as "edge"

SASAKI = 808963


def add_edge_count(agg):
    """approximate edge: pitch center within EDGE_BAND of the zone boundary
    (sides, top, or bottom). aggregated to a count per group and merged on."""
    p = pd.read_parquet(CLEAN, columns=[
        "pitcher", "pitch_type", "game_pk", "stand",
        "plate_x", "plate_z", "sz_top", "sz_bot"])
    x = p["plate_x"].abs()
    z, top, bot = p["plate_z"], p["sz_top"], p["sz_bot"]

    near_side = (x >= PLATE_HALF - EDGE_BAND) & (x <= PLATE_HALF + EDGE_BAND) \
        & (z >= bot - EDGE_BAND) & (z <= top + EDGE_BAND)
    near_top = (z >= top - EDGE_BAND) & (z <= top + EDGE_BAND) & (x <= PLATE_HALF + EDGE_BAND)
    near_bot = (z >= bot - EDGE_BAND) & (z <= bot + EDGE_BAND) & (x <= PLATE_HALF + EDGE_BAND)
    p["is_edge"] = near_side | near_top | near_bot

    edge = (p.groupby(["pitcher", "pitch_type", "game_pk", "stand"], observed=True)["is_edge"]
              .sum().rename("edge_num").reset_index())
    return agg.merge(edge, on=["pitcher", "pitch_type", "game_pk", "stand"], how="left")


def safe_div(a, b):
    return a / b.replace(0, np.nan)


def main():
    df = pd.read_parquet(AGG)
    df = add_edge_count(df)

    # per-start display rates
    df["whiff_pct"]   = safe_div(df["whiffs"], df["swings"]) * 100
    df["csw_pct"]     = safe_div(df["called"] + df["whiffs"], df["n_pitches"]) * 100
    df["chase_pct"]   = safe_div(df["chase_num"], df["ooz"]) * 100
    df["zone_pct"]    = safe_div(df["in_zone"], df["n_pitches"]) * 100
    df["edge_pct"]    = safe_div(df["edge_num"], df["n_pitches"]) * 100
    df["putaway_pct"] = safe_div(df["k"], df["two_strike"]) * 100
    df["k_pct"]       = safe_div(df["k"], df["pa"]) * 100
    df["usage_pct"]   = safe_div(df["n_pitches"], df["game_pitches"]) * 100
    df["hardhit_pct"] = safe_div(df["hardhits"], df["bbe"]) * 100
    df["barrel_pct"]  = safe_div(df["barrels"], df["bbe"]) * 100
    df["xwoba"]       = safe_div(df["xwoba_num"], df["woba_den"])
    df["woba"]        = safe_div(df["woba_num"], df["woba_den"])

    # iso approximation: extra bases / (bbe + k). see docstring caveat.
    tb_extra = df["e_2b"] + 2 * df["e_3b"] + 3 * df["e_hr"]
    df["iso"] = safe_div(tb_extra, df["bbe"] + df["k"])

    OUT.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(OUT, index=False)

    # validation: season rollup for one arm, sum counts THEN divide
    v = df[df["pitcher"] == SASAKI]
    if not v.empty:
        r = v.groupby("pitch_type", observed=True).agg(
            n=("n_pitches", "sum"), sw=("swings", "sum"), wh=("whiffs", "sum"),
            two=("two_strike", "sum"), k=("k", "sum"),
            bbe=("bbe", "sum"), hh=("hardhits", "sum"),
            xn=("xwoba_num", "sum"), wd=("woba_den", "sum")).reset_index()
        r["whiff%"]   = (r.wh / r.sw * 100).round(1)
        r["putaway%"] = (r.k / r.two * 100).round(1)
        r["hardhit%"] = (r.hh / r.bbe * 100).round(1)
        r["xwoba"]    = (r.xn / r.wd).round(3)
        print("sasaki season rollup (validate vs savant arsenal):")
        print(r[["pitch_type", "n", "whiff%", "putaway%", "hardhit%", "xwoba"]].to_string(index=False))

    print("wrote", OUT)


if __name__ == "__main__":
    main()
