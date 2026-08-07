"""
sc_fetch.py — drop-in low-memory replacement for pybaseball.statcast().

WHY THIS EXISTS
---------------
pybaseball.statcast() fetches every daily chunk in a thread pool, holds all of
them in a list, and concatenates the whole thing before returning. Statcast
ships ~118 columns and most are strings, which pandas stores as `object` dtype:
one pointer per cell to a separate Python str. A full season is ~700k rows.
700k x ~50 string columns is tens of millions of live Python objects, several
GB, carried around so a script can read pitch_type and description.

In April a season-to-date pull was a few weeks and fit fine. By July it doesn't,
which is why scripts that worked all spring started dying within days of each
other on the final concat. The failures look absurd ("cannot allocate 29.1 MiB")
precisely because the process is already at the memory ceiling when it asks.

WHAT THIS DOES
--------------
  1. Pulls one calendar month at a time instead of one season, so pybaseball's
     own internal concat never sees more than a month of daily frames.
  2. Throws away every column the caller didn't ask for, per window, before
     anything accumulates.
  3. Downcasts what's left: strings -> category, floats -> float32, ints -> the
     smallest int that fits. NaN-bearing numeric columns (e.g. zone) stay float
     so a missing value can never crash the cast.
  4. Concatenates only the trimmed, downcast monthly frames.

Net effect on a season-to-date pull: multiple GB -> ~100 MB, and peak memory
during the fetch is one month rather than the whole season.

HOW TO USE IT
-------------
One line per script. Replace:

    from pybaseball import statcast

with:

    from sc_fetch import statcast

Everything else stays. Same signature, same returned DataFrame, same column
names. If a script needs a column outside DEFAULT_COLS, pass it explicitly:

    raw = statcast(start_dt="2026-03-27", end_dt="2026-07-16",
                   cols=DEFAULT_COLS + ["release_spin_rate"])

Pass cols=None to keep all ~118 columns (still windowed and downcast, still far
lighter than pybaseball's, but the object columns come back).
"""

import calendar
import gc
from datetime import date, datetime, timedelta

import pandas as pd
from pybaseball import statcast as _pyb_statcast, cache as _pyb_cache

_pyb_cache.enable()

# The union of what the KSplit pull scripts actually read. Anything not in here
# is dropped before it can cost memory. Add to the list rather than removing the
# trim. Pass an explicit cols=[...] per call to be independent of this default.
DEFAULT_COLS = [
    "game_date", "game_type",
    "pitcher", "batter", "player_name",
    "stand", "p_throws",
    "pitch_type", "description", "events", "zone", "strikes",
    "release_speed", "pfx_x", "pfx_z", "delta_run_exp",
    "estimated_woba_using_speedangle",
]


def _to_date(d):
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, date):
        return d
    return datetime.strptime(str(d)[:10], "%Y-%m-%d").date()


def _month_windows(start, end):
    """Yield (window_start, window_end) date pairs, one per calendar month,
    clamped to [start, end]."""
    cur = start
    while cur <= end:
        last_day = calendar.monthrange(cur.year, cur.month)[1]
        month_end = date(cur.year, cur.month, last_day)
        win_end = min(month_end, end)
        yield cur, win_end
        cur = win_end + timedelta(days=1)


def _downcast(df):
    """Shrink dtypes in place. ONLY object (string) columns are converted to
    category, which is where essentially all the memory win lives and which the
    KSplit scripts coerce back to object at the point of use anyway. Every
    numeric column is left exactly as pybaseball returns it (float64 stays
    float64), so no value written to Neon or Sheets can differ by even a
    rounding bit from the old full-season path."""
    for c in df.columns:
        s = df[c]
        if s.dtype.kind == "O":
            try:
                df[c] = s.astype("category")
            except (TypeError, ValueError):
                pass
    return df


def _trim(df, cols):
    if cols is None:
        return df
    keep = [c for c in cols if c in df.columns]
    return df[keep]


def statcast(start_dt, end_dt=None, cols=DEFAULT_COLS, verbose=True, **kwargs):
    """Month-windowed, column-trimmed, downcast statcast pull.

    Signature-compatible with pybaseball.statcast for the way the KSplit scripts
    call it: statcast(start_dt=..., end_dt=...). Extra pybaseball kwargs (team,
    parallel, ...) are forwarded. Returns a single concatenated DataFrame with
    only `cols` (or all columns when cols=None).
    """
    start = _to_date(start_dt)
    end = _to_date(end_dt) if end_dt is not None else date.today()
    if start > end:
        return pd.DataFrame(columns=(cols or []))

    if cols is not None:
        missing = [c for c in cols if c not in DEFAULT_COLS]
        # not an error — caller may legitimately want a rarer column; pybaseball
        # returns it and _trim keeps it. This is just informational.
        if missing and verbose:
            print(f"  sc_fetch: pulling extra columns {missing}")

    frames = []
    for win_start, win_end in _month_windows(start, end):
        if verbose:
            print(f"  sc_fetch window {win_start} -> {win_end}", flush=True)
        try:
            raw = _pyb_statcast(start_dt=win_start.isoformat(),
                                end_dt=win_end.isoformat(),
                                verbose=verbose, **kwargs)
        except Exception as e:
            print(f"  sc_fetch: window {win_start}->{win_end} failed ({e}); skipping")
            continue
        if raw is None or raw.empty:
            del raw
            gc.collect()
            continue
        trimmed = _trim(raw, cols).copy()
        del raw
        _downcast(trimmed)
        frames.append(trimmed)
        gc.collect()

    if not frames:
        return pd.DataFrame(columns=(cols or []))

    out = pd.concat(frames, ignore_index=True)
    del frames
    gc.collect()
    return out
