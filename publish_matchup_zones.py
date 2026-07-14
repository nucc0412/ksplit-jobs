"""
publish_matchup_zones.py — capture the zone-map data for today's slate into Neon
(matchup_zone), for the Matchup Board's location maps.

Two sides, one table (see matchup_zone_schema.sql):
  pitcher side  — from Pitcher_Zone (freq) / _SwStr / _PutAway, keyed by exact pitch.
  hitter  side  — from Hitter_Zone_{CSW,SwStr,PutAway}_vs_{LHP,RHP}, keyed by family,
                  filtered to the nine confirmed hitters facing each starter.

Unlike the board publisher this does NOT drive B3/B5: the zone tabs are keyed by
pitcher/hitter across the whole league, so we pull each tab once and filter in Python.
That makes it fast and safe to run beside the board job on the same schedule.

Slate + lineups both come from 'Pitcher K Sim': each starter's row carries the
nine hitter names (cols K..S), their bats (cols T..AB), in batting order.

Run:
    python publish_matchup_zones.py           # dry run: build + counts, no write
    python publish_matchup_zones.py --write    # upsert into matchup_zone
    python publish_matchup_zones.py --write --only "paul skenes"

Requires: gspread, google-auth, psycopg[binary], python-dotenv (db.py).
"""

import argparse
import os
import re
import sys
import unicodedata
from datetime import date

import gspread
from google.oauth2.service_account import Credentials

from db import fetch, execute, get_conn

# ============================== CONFIG ========================================
CREDS_PATH = os.environ.get("KSPLIT_CREDS_PATH", r"C:\KSplit\credentials.json")
SHEET_NAME = "KSplit V1.0 2026 SZN"
SIM_TAB    = "Pitcher K Sim"

SIM_PITCHER_COL = 2     # B  starter display name
SIM_HAND_COL    = 4     # D  starter throwing hand (L/R)
SIM_KEY_COL     = 99    # CU normalized starter key
SIM_H1_NAME     = 11    # K  Hitter1 Name  (nine names run K..S = 11..19)
SIM_H1_BATS     = 20    # T  Hitter1 Bats  (nine bats  run T..AB = 20..28)

# Pitch code -> family. FC (cutter) is Fastball, per the pipeline that built the
# hitter family tabs. KC (knuckle-curve) and SV (slurve) are Breaking.
PITCH_FAMILY = {
    "FF": "FB", "SI": "FB", "FC": "FB",
    "CH": "OS", "FS": "OS",
    "SL": "BR", "ST": "BR", "CU": "BR", "KC": "BR", "SV": "BR",
}

PITCHER_ZONE_TABS = {                 # tab -> metric column name in matchup_zone
    "Pitcher_Zone":         "freq_rate",
    "Pitcher_Zone_SwStr":   "swstr_rate",
    "Pitcher_Zone_PutAway": "putaway_rate",
}
# hitter tabs are per hand; metric -> {effective hand: tab}
HITTER_ZONE_TABS = {
    "csw_rate":     {"L": "Hitter_Zone_CSW_vs_LHP",     "R": "Hitter_Zone_CSW_vs_RHP"},
    "swstr_rate":   {"L": "Hitter_Zone_SwStr_vs_LHP",   "R": "Hitter_Zone_SwStr_vs_RHP"},
    "putaway_rate": {"L": "Hitter_Zone_PutAway_vs_LHP", "R": "Hitter_Zone_PutAway_vs_RHP"},
}
# =============================================================================


def norm_key(name):
    if not isinstance(name, str):
        return ""
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", s.lower())


def num(v):
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return v
    try:
        return float(v)
    except (ValueError, TypeError):
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


def effective_hand(bats, pitcher_hand):
    """Which vs-hand hitter tab to read. A switch hitter ('S'/'B') bats opposite the
    pitcher's throwing hand, so vs a RHP he's effectively L, vs a LHP effectively R.
    L/R hitters use their own side. Defaults to R if unknown."""
    b = (bats or "").strip()[:1].upper()
    if b in ("S", "B"):
        return "L" if (pitcher_hand or "R").upper().startswith("R") else "R"
    return "L" if b == "L" else "R"


def load_slate(sh):
    """Read Pitcher K Sim once. Returns a list of dicts, one per starter:
    {name, key, hand, hitters:[(slot, name, key, bats, eff_hand), ...]}."""
    grid = sh.worksheet(SIM_TAB).get_all_values()
    out = []
    for row in grid[1:]:
        if len(row) < SIM_PITCHER_COL:
            continue
        name = row[SIM_PITCHER_COL - 1].strip()
        if not name:
            continue
        key = (row[SIM_KEY_COL - 1].strip() if len(row) >= SIM_KEY_COL and row[SIM_KEY_COL - 1]
               else name).strip()
        hand = (row[SIM_HAND_COL - 1].strip()[:1].upper() if len(row) >= SIM_HAND_COL else "R") or "R"
        hitters = []
        for i in range(9):
            ni = SIM_H1_NAME - 1 + i
            bi = SIM_H1_BATS - 1 + i
            hname = row[ni].strip() if ni < len(row) else ""
            if not hname:
                continue
            bats = row[bi].strip()[:1].upper() if bi < len(row) else ""
            hitters.append((i + 1, hname, norm_key(hname), bats, effective_hand(bats, hand)))
        out.append({"name": name, "key": norm_key(key), "hand": hand, "hitters": hitters})
    return out


def _idx(letter):
    n = 0
    for ch in letter:
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def load_pitcher_zone(sh, tab, want_keys):
    """Rows from a pitcher zone tab, filtered to want_keys (normalized starter keys).
    Columns: B PKey, D Pitch, E Bats, F Zone, G Pitches, H rate.
    Returns {(pkey, pitch, bats, zone): (family, n, rate)}."""
    grid = sh.worksheet(tab).get_all_values()
    out = {}
    for row in grid[1:]:
        pk = norm_key(row[_idx("B")]) if len(row) > _idx("B") else ""
        if pk not in want_keys:
            continue
        pitch = (row[_idx("D")].strip().upper() if len(row) > _idx("D") else "")
        fam = PITCH_FAMILY.get(pitch)
        if not fam:
            continue                                  # unknown pitch code -> skip
        bats = (row[_idx("E")].strip()[:1].upper() if len(row) > _idx("E") else "")
        zone = num(row[_idx("F")]) if len(row) > _idx("F") else None
        nct  = num(row[_idx("G")]) if len(row) > _idx("G") else None
        rate = num(row[_idx("H")]) if len(row) > _idx("H") else None
        if zone is None:
            continue
        out[(pk, pitch, bats, int(zone))] = (fam, nct, rate)
    return out


def load_hitter_zone(sh, tab, want_hitter_keys):
    """Rows from a hitter zone tab, filtered to the confirmed hitters.
    Columns: A Key(name|family|zone), B Name, C Bats, D Family, E Zone, F Pitches, G rate.
    Returns {(hkey, family, zone): (bats, n, rate)}."""
    grid = sh.worksheet(tab).get_all_values()
    out = {}
    for row in grid[1:]:
        name = row[_idx("B")].strip() if len(row) > _idx("B") else ""
        hk = norm_key(name)
        if hk not in want_hitter_keys:
            continue
        bats = (row[_idx("C")].strip()[:1].upper() if len(row) > _idx("C") else "")
        fam  = (row[_idx("D")].strip().upper() if len(row) > _idx("D") else "")
        zone = num(row[_idx("E")]) if len(row) > _idx("E") else None
        nct  = num(row[_idx("F")]) if len(row) > _idx("F") else None
        rate = num(row[_idx("G")]) if len(row) > _idx("G") else None
        if fam not in ("FB", "BR", "OS") or zone is None:
            continue
        out[(hk, fam, int(zone))] = (bats, nct, rate)
    return out


def build_rows(sh, slate, today):
    """Assemble matchup_zone rows for the whole slate. Returns (rows, stats)."""
    pitcher_keys = {p["key"] for p in slate}

    # --- pitcher side: pull each metric tab once, merge on the cell key ---
    merged = {}   # (pk,pitch,bats,zone) -> {family, n, freq, swstr, putaway}
    for tab, metric in PITCHER_ZONE_TABS.items():
        block = load_pitcher_zone(sh, tab, pitcher_keys)
        for cellkey, (fam, nct, rate) in block.items():
            rec = merged.setdefault(cellkey, {"family": fam, "n": nct,
                                              "freq_rate": None, "swstr_rate": None,
                                              "putaway_rate": None})
            rec[metric] = rate
            if rec["n"] is None:
                rec["n"] = nct
    pitcher_rows = []
    for (pk, pitch, bats, zone), rec in merged.items():
        pitcher_rows.append((today, pk, "pitcher", None, None, None, bats, pitch,
                             rec["family"], zone, rec["n"],
                             rec["freq_rate"], None, rec["swstr_rate"], rec["putaway_rate"]))

    # --- hitter side: pull each hitter tab (per hand) once, then assemble per lineup ---
    all_hitter_keys = {h[2] for p in slate for h in p["hitters"]}
    hitter_blocks = {}   # metric -> {hand -> {(hk,fam,zone): (bats,n,rate)}}
    for metric, hands in HITTER_ZONE_TABS.items():
        hitter_blocks[metric] = {h: load_hitter_zone(sh, tab, all_hitter_keys)
                                 for h, tab in hands.items()}

    hitter_rows = []
    for p in slate:
        pk = p["key"]
        for slot, hname, hk, bats, eff in p["hitters"]:
            merged_h = {}   # (fam,zone) -> {n, csw, swstr, putaway}
            for metric, hands in hitter_blocks.items():
                for (bk, fam, zone), (bt, nct, rate) in hands.get(eff, {}).items():
                    if bk != hk:
                        continue
                    rec = merged_h.setdefault((fam, zone),
                                              {"n": nct, "csw_rate": None,
                                               "swstr_rate": None, "putaway_rate": None})
                    rec[metric] = rate
                    if rec["n"] is None:
                        rec["n"] = nct
            for (fam, zone), rec in merged_h.items():
                hitter_rows.append((today, pk, "hitter", hk, hname, slot, bats, None,
                                    fam, zone, rec["n"],
                                    None, rec["csw_rate"], rec["swstr_rate"], rec["putaway_rate"]))

    stats = {"pitchers": len(pitcher_keys),
             "pitcher_cells": len(pitcher_rows),
             "hitters": len(all_hitter_keys),
             "hitter_cells": len(hitter_rows)}
    return pitcher_rows + hitter_rows, stats


COLS = ["slate_date", "pitcher_key", "side", "hitter_key", "hitter_name",
        "lineup_slot", "bats", "pitch", "family", "zone", "n",
        "freq_rate", "csw_rate", "swstr_rate", "putaway_rate"]


def insert_rows(table, columns, rows, page=1000):
    """Plain bulk INSERT (no conflict handling). Used after the slate's rows are
    DELETEd, so there is nothing to conflict with. Returns rows written."""
    if not rows:
        return 0
    cols = ", ".join(f'"{c}"' for c in columns)
    ph = "(" + ", ".join(["%s"] * len(columns)) + ")"
    query = f'INSERT INTO "{table}" ({cols}) VALUES {ph}'
    written = 0
    with get_conn() as conn:
        with conn.cursor() as cur:
            for i in range(0, len(rows), page):
                chunk = rows[i:i + page]
                cur.executemany(query, chunk)
                written += len(chunk)
    return written


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--only", default=None)
    args = ap.parse_args()

    sh = connect()
    slate = load_slate(sh)
    if args.only:
        want = norm_key(args.only)
        slate = [p for p in slate if p["key"] == want or norm_key(p["name"]) == want]
        if not slate:
            print(f"'{args.only}' not on today's slate."); return

    today = date.today().isoformat()
    rows, stats = build_rows(sh, slate, today)

    print(f"Zone capture for {today}: {stats['pitchers']} starters, "
          f"{stats['pitcher_cells']} pitcher cells; {stats['hitters']} hitters, "
          f"{stats['hitter_cells']} hitter cells; {len(rows)} rows total.")

    no_lineup = [p["name"] for p in slate if not p["hitters"]]
    if no_lineup:
        print(f"  note: no hitters listed for {len(no_lineup)} starter(s): {no_lineup} "
              f"(hitter maps skipped for those; pitcher maps still built)")

    if not args.write:
        print("DRY RUN — re-run with --write to upsert into matchup_zone.")
        if rows:
            print("  sample row:", dict(zip(COLS, rows[0])))
        return

    if not rows:
        print("Nothing to write."); return

    # Delete-then-insert: we clear this slate's rows above, so there are no
    # conflicting rows to update. A plain INSERT avoids needing an ON CONFLICT
    # target that exactly matches the table's (COALESCE-based) unique index.
    keys = list({p["key"] for p in slate})
    execute("DELETE FROM matchup_zone WHERE slate_date=%s AND pitcher_key = ANY(%s)",
            (today, keys))
    n = insert_rows("matchup_zone", COLS, rows)
    print(f"  upserted {n} zone rows into matchup_zone")
    chk = fetch("SELECT count(*) c FROM matchup_zone WHERE slate_date=%s", (today,))
    print(f"  matchup_zone now holds {chk[0]['c']} rows for {today}")
    print("Done.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
