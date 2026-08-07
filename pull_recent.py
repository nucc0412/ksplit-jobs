"""
pull_recent.py -- nightly incremental pull.

pulls the last N days of statcast league-wide and writes the one parquet the
rest of the pipeline reads. downstream is an idempotent upsert, so a few days
of overlap just refreshes those starts. this is what the nightly cron runs
instead of the full-season pull_pitch_shapes.py.

    python pull_recent.py
"""

import datetime as dt
from pathlib import Path

from sc_fetch import statcast

LOOKBACK_DAYS = 4
CACHE_DIR = Path("data/raw_statcast")

COLS = [
    "game_pk", "game_date", "game_type",
    "pitcher", "batter", "player_name", "stand", "p_throws",
    "pitch_type", "description", "events", "type", "zone", "balls", "strikes",
    "release_speed", "pfx_x", "pfx_z", "release_spin_rate", "spin_axis",
    "release_extension", "release_pos_x", "release_pos_z",
    "plate_x", "plate_z", "sz_top", "sz_bot",
    "launch_speed", "launch_angle", "launch_speed_angle",
    "estimated_woba_using_speedangle", "woba_value", "woba_denom",
    "delta_run_exp",
    "home_team", "away_team", "inning_topbot",
]


def main():
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    end = dt.date.today()
    start = end - dt.timedelta(days=LOOKBACK_DAYS)
    print(f"pulling {start} -> {end}")

    df = statcast(start_dt=start.isoformat(), end_dt=end.isoformat(), cols=COLS, verbose=False)
    if df is None or df.empty:
        print("no rows in window; nothing to update")
        return

    # clear any leftover parquet so normalize only sees this window
    for f in CACHE_DIR.glob("league_*.parquet"):
        f.unlink()

    out = CACHE_DIR / "league_recent.parquet"
    df.to_parquet(out, index=False)
    print(f"wrote {len(df)} pitches -> {out.name}")


if __name__ == "__main__":
    main()
