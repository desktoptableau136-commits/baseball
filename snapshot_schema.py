"""snapshot_schema.py -- validate data/snapshot.json against the reader contract.

`fetch_data.py` WRITES the snapshot; `send_digest.py`, `dashboard.py`, and
`weekly_recap.py` READ it blindly (each does `json.load` then reaches for keys/fields
with no shape checks). This module is the one place that asserts the shape all three
depend on, so an upstream break (an ESPN / FantasyPros / pybaseball column drop, a merge
that silently loses a field) fails LOUD at fetch time instead of quietly producing a
garbled -- or plausible-but-wrong -- digest.

Two severities:
  * ERROR  -- a reader would crash or read garbage: a missing spine key, an empty core
             list (fetch failed), an unparseable refreshed_at, a spine key of the wrong
             type. `assert_valid` RAISES on any error.
  * WARN   -- degraded but survivable (readers guard these with `.get()` fallbacks): a
             missing non-spine key, a wrong-type non-spine key, poor row-field coverage,
             or a NaN leak where the pipeline should have written the -1 sentinel.

`validate_snapshot(snap)` never raises -- it returns `(errors, warns)` so callers choose
the policy: fetch_data raises (keeps the previous good snapshot); the readers just print.

ASCII-only output -- fetch_data.py runs on Windows where charmap crashes on non-ASCII.

CLI:  python snapshot_schema.py [path]     # defaults to data/snapshot.json
"""

import json
import sys
from datetime import datetime
from pathlib import Path


class SnapshotValidationError(Exception):
    """Raised by assert_valid() when a snapshot violates the ERROR-level contract."""


# ---------------------------------------------------------------------------
# The contract. Keys/types mirror fetch_data.main()'s snapshot dict and the union
# of what the three readers touch (grep `snap.get(`/`snap[` across the repo).
# ---------------------------------------------------------------------------

# Spine: absence, wrong type, or (for lists) emptiness means the fetch fundamentally
# failed and every reader breaks. -> ERROR.
_SPINE = {
    "refreshed_at": str,
    "my_team":      str,
    "standings":    list,
    "pitchers":     list,
    "hitters":      list,
    "roto":         list,
}
# Core lists that must be NON-EMPTY (a populated fetch always has these). -> ERROR.
_NONEMPTY_LISTS = ("standings", "pitchers", "hitters", "roto")

# Everything else the readers touch. Present + right type is expected, but each reader
# guards these with `.get()` fallbacks, so a lapse is degraded-not-dead. -> WARN.
_EXPECTED = {
    "league_year":               int,
    "season_cat_totals":         dict,
    "weekly_results":            dict,
    "transactions":              list,   # legitimately empty on a quiet day
    "current_matchup":           dict,
    "prev_matchup":              dict,
    "all_matchups":              dict,
    "all_prev_matchups":         dict,
    "matchup_start_date":        str,
    "matchup_end_date":          str,
    "matchup_period_days":       int,
    "next_matchup_end_date":     str,
    "matchup_game_days":         int,
    "matchup_game_days_elapsed": int,
    "league_total_roster_max":   int,
    "recent_hitting":            list,
    "recent_pitching":           list,
    "prev_week_hitting":         list,
    "prev_week_pitching":        list,
    "lineup_efficiency":         dict,
    "lineup_efficiency_current": dict,
}

# Fields a well-formed row of each list carries. WARN if coverage drops below the floor
# (some rows legitimately lack a field, but a wholesale drop means a merge broke).
# NOTE: FantasyTeam is deliberately NOT here -- it's empty for the free-agent pool (the
# bulk of the rows), so a coverage floor on it fires a false positive. The listed fields
# are populated on every row regardless of roster status.
_ROW_FIELDS = {
    "pitchers":  ("PlayerName", "Position", "Dataset"),
    "hitters":   ("PlayerName", "Position", "Dataset"),
    "roto":      ("Team", "Week", "Roto_Score"),
    "standings": ("team_name",),
}
_ROW_COVERAGE_MIN = 0.90   # warn if < 90% of rows carry a required field


def _is_nan(v):
    """True for a float NaN (numpy or builtin). Missing numerics should be -1, not NaN."""
    return isinstance(v, float) and v != v


def validate_snapshot(snap):
    """Check a snapshot dict against the reader contract.

    Returns (errors, warns) -- two lists of human-readable ASCII strings. Never raises
    (a non-dict input is the one exception; that's a programmer error, not bad data).
    Works on both the in-memory dict inside fetch_data (pre-dump) and a re-loaded file
    (json round-trips NaN and the -1 sentinels identically).
    """
    if not isinstance(snap, dict):
        raise SnapshotValidationError(
            f"snapshot is {type(snap).__name__}, expected dict"
        )

    errors, warns = [], []

    # 1. Spine keys: present + right type.
    for key, typ in _SPINE.items():
        if key not in snap:
            errors.append(f"missing required key '{key}'")
        elif not isinstance(snap[key], typ):
            errors.append(f"key '{key}' is {type(snap[key]).__name__}, expected {typ.__name__}")

    # 2. Core lists non-empty.
    for key in _NONEMPTY_LISTS:
        v = snap.get(key)
        if isinstance(v, list) and not v:
            errors.append(f"core list '{key}' is empty (fetch likely failed)")

    # 3. refreshed_at ISO-parseable (the freshness badge parses it downstream).
    ra = snap.get("refreshed_at")
    if isinstance(ra, str) and ra:
        try:
            datetime.fromisoformat(ra)
        except ValueError:
            errors.append(f"refreshed_at not ISO-parseable: {ra!r}")

    # 4. Expected (non-spine) keys: present + right type -> WARN.
    for key, typ in _EXPECTED.items():
        if key not in snap:
            warns.append(f"missing expected key '{key}'")
        elif not isinstance(snap[key], typ):
            warns.append(f"key '{key}' is {type(snap[key]).__name__}, expected {typ.__name__}")

    # 5. Row-field coverage -> WARN.
    for key, fields in _ROW_FIELDS.items():
        rows = snap.get(key)
        if not isinstance(rows, list) or not rows:
            continue
        n = len(rows)
        for f in fields:
            have = sum(1 for r in rows if isinstance(r, dict) and r.get(f) not in (None, ""))
            if have < n * _ROW_COVERAGE_MIN:
                warns.append(
                    f"'{key}': field '{f}' present on {have}/{n} rows "
                    f"({have / n:.0%} < {_ROW_COVERAGE_MIN:.0%})"
                )

    # 6. NaN leaks in player rows -> WARN (missing numerics must be the -1 sentinel).
    for key in ("pitchers", "hitters"):
        rows = snap.get(key)
        if not isinstance(rows, list):
            continue
        nan_counts = {}
        for r in rows:
            if not isinstance(r, dict):
                continue
            for fld, v in r.items():
                if _is_nan(v):
                    nan_counts[fld] = nan_counts.get(fld, 0) + 1
        for fld, cnt in sorted(nan_counts.items(), key=lambda kv: -kv[1]):
            warns.append(f"'{key}': NaN in field '{fld}' on {cnt} row(s) (should be -1 sentinel)")

    return errors, warns


def report(errors, warns, out=print):
    """Print an ASCII summary of validate_snapshot() output."""
    out("=== snapshot validation ===")
    for e in errors:
        out(f"  [ERROR] {e}")
    for w in warns:
        out(f"  [WARN]  {w}")
    if not errors and not warns:
        out("  [OK] no issues")
    else:
        out(f"  ({len(errors)} error(s), {len(warns)} warning(s))")


def assert_valid(snap, verbose=True, strict=False):
    """Validate and RAISE SnapshotValidationError on any ERROR (or any WARN if strict).

    Prints a report when verbose. Returns the warns list on success. This is the
    fetch-time guard: raising leaves the previous good snapshot untouched.
    """
    errors, warns = validate_snapshot(snap)
    if verbose:
        report(errors, warns)
    if errors:
        head = "; ".join(errors[:5])
        more = f" (+{len(errors) - 5} more)" if len(errors) > 5 else ""
        raise SnapshotValidationError(f"{len(errors)} error(s): {head}{more}")
    if strict and warns:
        raise SnapshotValidationError(f"{len(warns)} warning(s) in strict mode")
    return warns


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    path = Path(argv[0]) if argv else Path(__file__).parent / "data" / "snapshot.json"
    if not path.exists():
        print(f"[ERROR] snapshot not found: {path}")
        return 2
    with open(path) as f:
        snap = json.load(f)
    try:
        assert_valid(snap, verbose=True)
    except SnapshotValidationError as e:
        print(f"[FAIL] {e}")
        return 1
    print("[OK] snapshot valid")
    return 0


if __name__ == "__main__":
    sys.exit(main())
