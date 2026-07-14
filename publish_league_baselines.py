"""
publish_league_baselines.py — capture the league-average baselines into Neon so the
website can color every metric (tables and zone maps) against league.

Sources landing in two tables (see league_baseline_schema.sql):
  Per-pitch  -> league_pitch_baseline  (metric, pitch, vs_hand, rate, n)
     csw/swstr/putaway/whiff/chase from the simple 5-col League_*_Baselines tabs,
     plus iso + xwoba from the multi-column League_Outcome_Baselines tab.
     (OPS has no league baseline tab, so OPS stays uncolored on the site.)
  Location   -> league_loc_baseline    (metric, family, region, throws, rate, fam_overall_rate, n)
     League_SwStr_Loc_Baselines / League_PutAway_Loc_Baselines

These barely change, so run this on a slow cadence (e.g. weekly), not per slate.
Rows with zero sample (e.g. the dead 'SW' pitch row) are skipped.

Run:
    python publish_league_baselines.py          # dry run
    python publish_league_baselines.py --write   # write both tables

Requires: gspread, google-auth, psycopg[binary], python-dotenv (db.py).
"""

import argparse
import os
import sys
from datetime import datetime

import gspread
from google.oauth2.service_account import Credentials

from db import fetch, execute, get_conn

CREDS_PATH = os.environ.get("KSPLIT_CREDS_PATH", r"C:\KSplit\credentials.json")
SHEET_NAME = "KSplit V1.0 2026 SZN"

PITCH_TABS = {                                  # tab -> metric (simple A=Pitch,B=vL,C=vR,D=vLN,E=vRN shape)
    "League_CSW_Baselines":     "csw",
    "League_SwStr_Baselines":   "swstr",
    "League_PutAway_Baselines": "putaway",
    "League_Whiff_Baselines":   "whiff",
    "League_Chase_Baselines":   "chase",
}
# ISO lives in the multi-column League_Outcome_Baselines tab, handled separately.
OUTCOME_TAB = "League_Outcome_Baselines"
LOC_TABS = {                                    # tab -> metric
    "League_SwStr_Loc_Baselines":   "swstr",
    "League_PutAway_Loc_Baselines": "putaway",
}


def numf(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def connect():
    scopes = ["https://www.googleapis.com/auth/spreadsheets",
              "https://www.googleapis.com/auth/drive"]
    raw = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if raw:
        import json
        creds = Credentials.from_service_account_info(json.loads(raw), scopes=scopes)
    else:
        creds = Credentials.from_service_account_file(CREDS_PATH, scopes=scopes)
    return gspread.authorize(creds).open(SHEET_NAME)


def load_pitch(sh):
    """Rows for league_pitch_baseline. One row per (metric, pitch, vs_hand).
    Covers the simple 5-col tabs (csw/swstr/putaway/whiff/chase) plus ISO and
    xwOBA pulled from the multi-column League_Outcome_Baselines tab."""
    rows = []
    for tab, metric in PITCH_TABS.items():
        grid = sh.worksheet(tab).get_all_values()
        for r in grid[1:]:                       # A Pitch, B vL rate, C vR rate, D vL N, E vR N
            pitch = (r[0].strip().upper() if len(r) > 0 else "")
            if not pitch:
                continue
            for hand, ri, ni in (("L", 1, 3), ("R", 2, 4)):
                rate = numf(r[ri]) if len(r) > ri else None
                n    = numf(r[ni]) if len(r) > ni else None
                if rate is None or not n:         # skip missing and zero-sample (dead SW row)
                    continue
                rows.append((metric, pitch, hand, rate, int(n)))

    # ISO + xwOBA from League_Outcome_Baselines (0-based col indexes):
    #   A0 Pitch | F5 vL_ISO G6 N | H7 vR_ISO I8 N | J9 vL_xwOBA K10 N | L11 vR_xwOBA M12 N
    outcome_map = {
        "iso":   {"L": (5, 6),  "R": (7, 8)},
        "xwoba": {"L": (9, 10), "R": (11, 12)},
    }
    grid = sh.worksheet(OUTCOME_TAB).get_all_values()
    for r in grid[1:]:
        pitch = (r[0].strip().upper() if len(r) > 0 else "")
        if not pitch:
            continue
        for metric, hands in outcome_map.items():
            for hand, (ri, ni) in hands.items():
                rate = numf(r[ri]) if len(r) > ri else None
                n    = numf(r[ni]) if len(r) > ni else None
                if rate is None or not n:
                    continue
                rows.append((metric, pitch, hand, rate, int(n)))
    return rows


def load_loc(sh):
    """Rows for league_loc_baseline. One row per (metric, family, region, throws)."""
    rows = []
    for tab, metric in LOC_TABS.items():
        grid = sh.worksheet(tab).get_all_values()
        for r in grid[1:]:      # A Family, B Region, C Throws, D rate, E N, F FamOverall
            fam    = (r[0].strip().upper() if len(r) > 0 else "")
            region = (r[1].strip().title() if len(r) > 1 else "")
            throws = (r[2].strip()[:1].upper() if len(r) > 2 else "")
            rate   = numf(r[3]) if len(r) > 3 else None
            n      = numf(r[4]) if len(r) > 4 else None
            famov  = numf(r[5]) if len(r) > 5 else None
            if fam not in ("FB", "BR", "OS") or not region or rate is None or not n:
                continue
            rows.append((metric, fam, region, throws, rate, famov, int(n)))
    return rows


def replace_table(table, columns, rows):
    """Full replace: clear the small reference table, then insert fresh."""
    execute(f"DELETE FROM {table}")
    if not rows:
        return 0
    cols = ", ".join(f'"{c}"' for c in columns)
    ph = "(" + ", ".join(["%s"] * len(columns)) + ")"
    query = f'INSERT INTO "{table}" ({cols}) VALUES {ph}'
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.executemany(query, rows)
    return len(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()

    sh = connect()
    pitch = load_pitch(sh)
    loc = load_loc(sh)
    print(f"Built {len(pitch)} per-pitch baseline rows, {len(loc)} location baseline rows.")

    if not args.write:
        print("DRY RUN — re-run with --write.")
        if pitch:
            print("  sample pitch:", pitch[0])
        if loc:
            print("  sample loc:  ", loc[0])
        return

    n1 = replace_table("league_pitch_baseline",
                       ["metric", "pitch", "vs_hand", "rate", "n"], pitch)
    n2 = replace_table("league_loc_baseline",
                       ["metric", "family", "region", "throws", "rate",
                        "fam_overall_rate", "n"], loc)
    print(f"  wrote {n1} rows to league_pitch_baseline, {n2} rows to league_loc_baseline")
    print("Done.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
