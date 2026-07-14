"""
publish_matchup_board.py — read the Research_Matchup board for each pitcher on
today's slate and publish it to Neon (matchup_board) as JSON for the website.

How it works: Research_Matchup shows ONE pitcher at a time. It is driven by TWO
cells that must move together:
    B3  the pitcher DISPLAY NAME ("Will Warren")  -> drives the lineup section
    B5  the lowercased NAME      ("will warren")  -> drives arsenal + detail lookups
B5 is the LOWER(TRIM(B3)) form: lowercase with spaces preserved, NOT the letters-
only norm_key. B5 does NOT auto-derive from B3 (there is no LOWER/TRIM formula in
it), so if the script sets only B3, B5 keeps the PREVIOUS pitcher's key and every
arsenal/detail
lookup resolves to the wrong pitcher (the "arsenal 0" symptom). This script writes
BOTH cells for each pitcher, waits for Google to recalc, VERIFIES both readbacks
switched, and only then reads the board. As a second guard, a board whose arsenal
is still empty after recalc is SKIPPED and never written, so an empty board can
never reach Neon.

Slate source: the pitchers in 'Pitcher K Sim' col B (names) / CU (normalized key).

Output: one row per pitcher in Neon table matchup_board, keyed on
(pitcher_key, slate_date), columns pitcher_key, pitcher_name, pitcher_hand,
slate_date, board (jsonb). The website reads these rows directly from Neon with a
read-only role and never recomputes anything. The search bar queries the light
list (pitcher_key, pitcher_name, pitcher_hand for today) and loads one pitcher's
board on selection.

A board whose arsenal is still empty after recalc is SKIPPED and never written, so
an empty board can never reach Neon.

Run (once lineups are set, before you're working in the sheet):
    python publish_matchup_board.py            # dry run: build + preview, no write
    python publish_matchup_board.py --write     # write to Neon
    python publish_matchup_board.py --write --only "paul skenes"   # one pitcher

Requires: gspread, google-auth, psycopg[binary], python-dotenv (db.py).
"""

import argparse
import json
import os
import re
import sys
import time
import unicodedata
from datetime import date, datetime

import gspread
from google.oauth2.service_account import Credentials

from db import upsert_rows, fetch

# ============================== CONFIG ========================================
# CREDS_PATH is used for LOCAL runs. In GitHub Actions we instead pass the whole
# service-account JSON in the GOOGLE_CREDENTIALS_JSON env var (see connect()).
CREDS_PATH = os.environ.get("KSPLIT_CREDS_PATH", r"C:\KSplit\credentials.json")
SHEET_NAME = "KSplit V1.0 2026 SZN"
SIM_TAB    = "Pitcher K Sim"
RM_TAB     = "Research_Matchup"

SIM_PITCHER_COL = 2    # B  pitcher name
SIM_KEY_COL     = 99   # CU normalized pitcher key

NAME_CELL   = "B3"   # driver 1: pitcher DISPLAY NAME (drives lineup section)
KEY_CELL    = "B5"   # driver 2: lowercased name, LOWER(TRIM(B3)) form (arsenal + detail lookups)
HAND_CELL   = "D3"
STATUS_CELL = "I3"
VULN_CELL   = "N46"  # most vulnerable
TOUGH_CELL  = "N47"  # toughest

RECALC_WAIT   = 3.0    # seconds to let Google recompute after writing B3/B5
RECALC_TRIES  = 8      # re-check B3/B5 until both match the requested pitcher
ARSENAL_TRIES = 3      # extra reads of the board while the arsenal block populates
# =============================================================================


def norm_key(name):
    if not isinstance(name, str):
        return ""
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", s.lower())


def lower_key(name):
    """Mirror the sheet's LOWER(TRIM(B3)): lowercase, trim, and collapse internal
    whitespace runs to single spaces. Keeps spaces/periods/hyphens (unlike norm_key,
    which strips everything to letters). This is the form B5 must hold."""
    if not isinstance(name, str):
        return ""
    return re.sub(r"\s+", " ", name).strip().lower()


def num(v):
    """Coerce a cell to float where possible, else keep string, else None."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return v
    try:
        return float(v)
    except (ValueError, TypeError):
        s = str(v).strip()
        return s or None


def connect():
    scopes = ["https://www.googleapis.com/auth/spreadsheets",
              "https://www.googleapis.com/auth/drive"]
    raw = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if raw:                                   # cloud run: creds come from a secret
        creds = Credentials.from_service_account_info(json.loads(raw), scopes=scopes)
    else:                                     # local run: creds come from the file
        creds = Credentials.from_service_account_file(CREDS_PATH, scopes=scopes)
    return gspread.authorize(creds).open(SHEET_NAME)


def slate_pitchers(sh):
    """(display_name, key) for every pitcher on today's Sim sheet."""
    ws = sh.worksheet(SIM_TAB)
    grid = ws.get_all_values()
    out = []
    for row in grid[1:]:
        if len(row) < SIM_PITCHER_COL:
            continue
        name = row[SIM_PITCHER_COL - 1].strip()
        if not name:
            continue
        key = (row[SIM_KEY_COL - 1].strip() if len(row) >= SIM_KEY_COL and row[SIM_KEY_COL - 1]
               else name).strip()
        out.append((name, norm_key(key)))
    return out


def _rows(ws_vals, r0, r1, cols):
    """Extract a block as list-of-dicts using the header names in `cols`
    (dict {letter: header}). Skips fully-empty rows."""
    def idx(letter):
        n = 0
        for ch in letter:
            n = n * 26 + (ord(ch) - 64)
        return n - 1
    out = []
    for r in range(r0, r1 + 1):
        row = ws_vals[r - 1] if r - 1 < len(ws_vals) else []
        rec = {}
        any_val = False
        for letter, key in cols.items():
            v = row[idx(letter)] if idx(letter) < len(row) else ""
            val = num(v)
            if val is not None and val != "":
                any_val = True
            rec[key] = val
        if any_val:
            out.append(rec)
    return out


LINEUP_COLS = {"A": "order", "B": "hitter", "C": "bats", "D": "csw", "E": "swstr",
               "F": "whiff", "G": "chase", "H": "putaway", "I": "ops", "J": "iso", "K": "xwoba"}
ARSENAL_COLS = {"A": "pitch", "B": "use_vR", "C": "use_vL", "D": "csw_vR", "E": "csw_vL",
                "F": "whiff_vR", "G": "whiff_vL", "H": "chase_vR", "I": "chase_vL",
                "J": "barrel_vR", "K": "barrel_vL", "L": "iso_vR", "M": "iso_vL",
                "N": "xwoba_vR", "O": "xwoba_vL"}
DETAIL_COLS = {"A": "hitter", "B": "pitch", "C": "use", "D": "csw", "E": "swstr",
               "F": "whiff", "G": "chase", "H": "putaway", "I": "ops", "J": "iso", "K": "xwoba"}
READ_COLS = {"M": "hitter", "N": "best_k_pitch", "O": "use", "P": "k_edge",
             "Q": "csw_vlg", "R": "swstr_vlg", "S": "whiff_vlg", "T": "chase_vlg",
             "U": "putaway_vlg", "V": "xwoba_vlg", "W": "tag"}


PITCH_FAMILY = {                       # same mapping the zone job uses
    "FF": "FB", "SI": "FB", "FC": "FB",
    "CH": "OS", "FS": "OS",
    "SL": "BR", "ST": "BR", "CU": "BR", "KC": "BR", "SV": "BR",
}


def family_weights(arsenal):
    """Roll per-pitch arsenal usage up to FB/BR/OS fractions, per batter hand.
    Returns {'L': {'FB':x,'BR':y,'OS':z}, 'R': {...}} normalized to sum 1 per hand.
    These are the weights the site uses to blend the hitter 'All families' map the
    same way the card does (usage-weighted vs the pitcher's arsenal)."""
    def f(v):                                   # usage cell -> float, 0 if non-numeric
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0
    out = {"L": {"FB": 0.0, "BR": 0.0, "OS": 0.0},
           "R": {"FB": 0.0, "BR": 0.0, "OS": 0.0}}
    for a in arsenal:
        fam = PITCH_FAMILY.get((a.get("pitch") or "").strip().upper())
        if not fam:
            continue
        out["L"][fam] += f(a.get("use_vL"))
        out["R"][fam] += f(a.get("use_vR"))
    for hand in ("L", "R"):
        tot = sum(out[hand].values())
        if tot:
            out[hand] = {fam: v / tot for fam, v in out[hand].items()}
    return out


def read_board(ws):
    """Read the currently-displayed board (three sections + summary)."""
    vals = ws.get_all_values()
    def cell(a1):
        col = re.match(r"[A-Z]+", a1).group()
        row = int(re.match(r"[A-Z]+(\d+)", a1).group(1))
        n = 0
        for ch in col:
            n = n * 26 + (ord(ch) - 64)
        r = vals[row - 1] if row - 1 < len(vals) else []
        return r[n - 1] if n - 1 < len(r) else ""
    lineup = _rows(vals, 7, 15, LINEUP_COLS)
    arsenal = [a for a in _rows(vals, 20, 30, ARSENAL_COLS)
               if (a.get("use_vR") or 0) or (a.get("use_vL") or 0)]   # drop 0-usage pitches
    detail = _rows(vals, 33, 130, DETAIL_COLS)   # 9 hitters x up to ~10 pitches; empty rows are skipped
    read = _rows(vals, 33, 41, READ_COLS)
    summary = {"lineup_status": cell(STATUS_CELL),
               "most_vulnerable": cell(VULN_CELL),
               "toughest": cell(TOUGH_CELL)}
    return {"lineup": lineup, "arsenal": arsenal, "detail": detail,
            "lineup_read": read, "summary": summary,
            "family_weights": family_weights(arsenal),
            "generated_at": datetime.now().isoformat()}


def read_board_ready(ws, tries=ARSENAL_TRIES):
    """Read the board, retrying a few times if the arsenal block hasn't populated
    yet (it depends on B5 and can lag the rest of the recalc). Returns the board;
    the caller checks board['arsenal'] to decide whether it is safe to publish."""
    board = read_board(ws)
    for _ in range(max(0, tries - 1)):
        if board["arsenal"]:
            break
        time.sleep(RECALC_WAIT)
        board = read_board(ws)
    return board


def _first(block):
    """First cell of a gspread batch_get value block, trimmed. '' if empty."""
    try:
        return (block[0][0] or "").strip()
    except (IndexError, TypeError):
        return ""


def set_and_wait(ws, display_name):
    """Drive the board: write the DISPLAY NAME to B3 and its LOWER(TRIM) form to B5
    together, wait for recalc, then verify B3 reads back the NAME WE JUST WROTE.
    We verify against norm_key(display_name) — the value we put in B3 — NOT the slate's
    CU key. Those two normalize differently for some pitchers, and comparing B3 to the
    CU key was failing pitchers whose board had already switched correctly (the clean
    data-ordered cliff mid-slate). B5 is written (it drives arsenal) but not read back;
    the arsenal guard in read_board_ready is the real board-built check.
    Returns (ok, display_name, hand)."""
    want = norm_key(display_name)
    ws.batch_update([
        {"range": NAME_CELL, "values": [[display_name]]},
        {"range": KEY_CELL,  "values": [[lower_key(display_name)]]},  # "paul skenes", drives arsenal
    ])
    got_name = ""
    for _ in range(RECALC_TRIES):
        time.sleep(RECALC_WAIT)
        blocks = ws.batch_get([NAME_CELL, HAND_CELL])
        got_name = _first(blocks[0]) if len(blocks) > 0 else ""
        got_hand = _first(blocks[1]) if len(blocks) > 1 else ""
        if norm_key(got_name) == want:
            return True, got_name, got_hand[:1]
    return False, got_name, ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--only", default=None, help="publish just one pitcher (by key or name)")
    args = ap.parse_args()

    sh = connect()
    ws = sh.worksheet(RM_TAB)
    slate = slate_pitchers(sh)
    if args.only:
        want = norm_key(args.only)
        slate = [(n, k) for (n, k) in slate if k == want or norm_key(n) == want]
        if not slate:
            print(f"'{args.only}' not on today's slate."); return

    print(f"Publishing {len(slate)} matchup board(s) for {date.today()} ...")
    today = date.today().isoformat()
    rows, skipped = [], []
    for i, (name, key) in enumerate(slate, 1):
        ok, disp, hand = set_and_wait(ws, name)   # write B3+B5, verify B3 == name we wrote
        if not ok:
            print(f"  [{i}/{len(slate)}] {name}: board did not switch (got '{disp}') — SKIPPED")
            skipped.append(name); continue
        board = read_board_ready(ws)
        if not board["arsenal"]:
            print(f"  [{i}/{len(slate)}] {disp}: arsenal empty after recalc — SKIPPED (nothing written)")
            skipped.append(disp); continue
        rows.append((key, disp, hand or None, today, json.dumps(board)))
        n_line = len(board["lineup"]); n_ars = len(board["arsenal"])
        print(f"  [{i}/{len(slate)}] {disp} ({hand}) — lineup {n_line}, arsenal {n_ars}, "
              f"status {board['summary']['lineup_status']}")

    if not args.write:
        print(f"\nDRY RUN — built {len(rows)} boards ({len(skipped)} skipped). Re-run with --write.")
        if rows:
            sample = json.loads(rows[0][4])
            print("  sample lineup[0]:", sample["lineup"][0] if sample["lineup"] else None)
            print("  sample arsenal[0]:", sample["arsenal"][0] if sample["arsenal"] else None)
        return

    if not rows:
        print("\nNothing to write — every board was skipped. Neon left untouched.")
        return

    n = upsert_rows("matchup_board",
                    ["pitcher_key", "pitcher_name", "pitcher_hand", "slate_date", "board"],
                    rows,
                    conflict_cols=["pitcher_key", "slate_date"],
                    update_cols=["pitcher_name", "pitcher_hand", "board"])
    print(f"\n  upserted {n} boards to matchup_board" + (f" ({len(skipped)} skipped: {skipped})" if skipped else ""))
    chk = fetch("SELECT count(*) c FROM matchup_board WHERE slate_date=%s", (today,))
    print(f"  matchup_board now holds {chk[0]['c']} boards for {today}")
    print("Done.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
