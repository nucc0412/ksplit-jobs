"""
normalize_pitches.py — step 3 (v2): clean + normalize, now deriving opponent.

a pitcher is on the HOME team when they pitch in the top of innings
(inning_topbot == 'Top'), so the opponent is the away team, and vice versa.
opp + is_home are carried per pitch for the tracker's date context.

    python normalize_pitches.py
"""

import unicodedata
from pathlib import Path

import numpy as np
import pandas as pd

RAW_DIR = Path("data/raw_statcast")
OUT = Path("data/clean/pitches_2026.parquet")

KEEP_GAME_TYPES = {"R"}
PITCH_KEEP = {"FF", "SI", "FC", "SL", "ST", "SV", "CU", "KC", "CH", "FS", "FO"}


def fold_name(s):
    if not isinstance(s, str):
        return s
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")


def load_raw():
    files = sorted(RAW_DIR.glob("league_*.parquet")) or sorted(RAW_DIR.glob("*.parquet"))
    if not files:
        raise SystemExit(f"no parquet in {RAW_DIR} - run pull_pitch_shapes.py first")
    return pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)


def main():
    df = load_raw()
    n0 = len(df)

    for c in ["game_type", "pitch_type", "stand", "p_throws", "player_name",
              "home_team", "away_team", "inning_topbot"]:
        if c in df.columns:
            df[c] = df[c].astype("string")

    df = df[df["game_type"].isin(KEEP_GAME_TYPES)]
    df = df.dropna(subset=["pitch_type", "release_speed", "pfx_x", "pfx_z"])
    df = df[df["pitch_type"].isin(PITCH_KEEP)]

    # shape
    df["ivb_in"] = df["pfx_z"] * 12.0
    arm_sign = df["p_throws"].map({"R": -1.0, "L": 1.0})
    df["hb_arm_in"] = df["pfx_x"] * 12.0 * arm_sign
    df["in_zone"] = df["zone"].between(1, 9)

    # opponent: pitcher is home when they pitch the top of innings
    is_home = df["inning_topbot"].eq("Top")
    df["is_home"] = is_home.fillna(False).astype(bool)
    df["opp"] = np.where(is_home, df["away_team"], df["home_team"])

    df["pitcher_name_disp"] = df["player_name"].map(fold_name)

    for c in ["game_pk", "pitcher", "batter"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], downcast="integer")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(OUT, index=False)

    print(f"in  {n0:>7} pitches")
    print(f"out {len(df):>7} pitches  ({n0 - len(df)} dropped)")
    print("pitch types:", sorted(df["pitch_type"].dropna().unique().tolist()))
    print("wrote", OUT)


if __name__ == "__main__":
    main()
