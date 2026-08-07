"""
aggregate_starts.py — step 4 (v2): collapse to per-start rows, carrying opp.

opp + is_home are constant within a pitcher's start, so they ride along as
first() per group.

    python aggregate_starts.py
"""

from pathlib import Path

import numpy as np
import pandas as pd

IN = Path("data/clean/pitches_2026.parquet")
OUT = Path("data/agg/starts_2026.parquet")

WHIFF = {"swinging_strike", "swinging_strike_blocked", "foul_tip"}
SWING = WHIFF | {"foul", "foul_bunt", "missed_bunt", "bunt_foul_tip", "hit_into_play"}


def main():
    df = pd.read_parquet(IN)

    for c in ["description", "events", "type", "stand", "opp"]:
        if c in df.columns:
            df[c] = df[c].astype("string")

    d, ev = df["description"], df["events"]

    df["is_swing"]  = d.isin(SWING)
    df["is_whiff"]  = d.isin(WHIFF)
    df["is_called"] = d.eq("called_strike")
    df["is_ooz"]    = df["zone"].between(11, 14)
    df["is_chase"]  = df["is_swing"] & df["is_ooz"]
    df["is_2k"]     = df["strikes"].eq(2)
    df["is_pa"]     = ev.notna()
    df["is_k"]      = ev.eq("strikeout")
    df["is_bbe"]    = df["type"].eq("X")
    df["is_barrel"] = df["launch_speed_angle"].eq(6)
    df["is_hard"]   = df["is_bbe"] & (df["launch_speed"] >= 95)
    df["is_1b"]     = ev.eq("single")
    df["is_2b"]     = ev.eq("double")
    df["is_3b"]     = ev.eq("triple")
    df["is_hr"]     = ev.eq("home_run")

    rad = np.deg2rad(df["spin_axis"].astype(float))
    df["axis_sin"], df["axis_cos"] = np.sin(rad), np.cos(rad)

    est = df["estimated_woba_using_speedangle"].astype(float)
    wv = df["woba_value"].astype(float)
    contrib = np.where(df["is_bbe"] & est.notna(), est, wv)
    df["xwoba_num"] = np.where(df["is_pa"], contrib, np.nan)

    keys = ["pitcher", "pitch_type", "game_pk", "stand"]
    g = df.groupby(keys, observed=True)

    out = g.agg(
        game_date    = ("game_date", "first"),
        pitcher_name = ("pitcher_name_disp", "first"),
        p_throws     = ("p_throws", "first"),
        opp          = ("opp", "first"),
        is_home      = ("is_home", "first"),
        velo   = ("release_speed", "mean"),
        ivb_in = ("ivb_in", "mean"),
        hb_in  = ("hb_arm_in", "mean"),
        spin   = ("release_spin_rate", "mean"),
        ext    = ("release_extension", "mean"),
        rel_x  = ("release_pos_x", "mean"),
        rel_z  = ("release_pos_z", "mean"),
        axis_sin = ("axis_sin", "sum"),
        axis_cos = ("axis_cos", "sum"),
        n_pitches  = ("release_speed", "size"),
        swings     = ("is_swing", "sum"),
        whiffs     = ("is_whiff", "sum"),
        called     = ("is_called", "sum"),
        in_zone    = ("in_zone", "sum"),
        ooz        = ("is_ooz", "sum"),
        chase_num  = ("is_chase", "sum"),
        two_strike = ("is_2k", "sum"),
        pa         = ("is_pa", "sum"),
        k          = ("is_k", "sum"),
        bbe        = ("is_bbe", "sum"),
        barrels    = ("is_barrel", "sum"),
        hardhits   = ("is_hard", "sum"),
        e_1b       = ("is_1b", "sum"),
        e_2b       = ("is_2b", "sum"),
        e_3b       = ("is_3b", "sum"),
        e_hr       = ("is_hr", "sum"),
        xwoba_num  = ("xwoba_num", "sum"),
        woba_num   = ("woba_value", "sum"),
        woba_den   = ("woba_denom", "sum"),
    ).reset_index()

    ang = np.rad2deg(np.arctan2(out["axis_sin"], out["axis_cos"]))
    out["axis"] = (ang + 360) % 360
    out = out.drop(columns=["axis_sin", "axis_cos"])

    game_tot = (df.groupby(["pitcher", "game_pk"], observed=True)
                  .size().rename("game_pitches").reset_index())
    hand_tot = (df.groupby(["pitcher", "game_pk", "stand"], observed=True)
                  .size().rename("game_pitches_hand").reset_index())
    out = out.merge(game_tot, on=["pitcher", "game_pk"], how="left")
    out = out.merge(hand_tot, on=["pitcher", "game_pk", "stand"], how="left")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    out.to_parquet(OUT, index=False)

    print(f"{len(df):>7} pitches -> {len(out):>6} rows (pitcher x pitch x start x hand)")
    print("wrote", OUT)


if __name__ == "__main__":
    main()
