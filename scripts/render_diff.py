#!/usr/bin/env python3
"""Golden-render diff harness for refactors.

Renders every offline preview from the existing snapshot and compares it
byte-for-byte against a saved baseline, so a pure refactor can prove "nothing
changed". Volatile fields (Trade Lab builtAt stamp) are normalized before
comparing; PYTHONHASHSEED is pinned because _emoji_avatar's fallback color
uses hash() (salted per process).

Usage (from the repo root):
    python scripts/render_diff.py baseline   # render + save as the baseline
    python scripts/render_diff.py check      # render + diff against the baseline

Exit 0 = all files byte-identical (after normalization); exit 1 = any diff,
with the first differing region printed for review. NOTE: compare with this
script (or cmp/python), NOT GNU diff — diff emits degenerate whole-file hunks
on the digest's very long (>20KB) lines.
"""
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE = ROOT / "previews_baseline"
PREVIEWS = ROOT / "previews"

# (command, files it writes that we track)
RENDERS = [
    (["python", "send_digest.py", "--dry-run", "--no-refresh"],
     ["digest_preview_*.html", "briefing_preview_*.html"]),
    (["python", "dashboard.py"], ["dashboard_*.html"]),
    (["python", "trade_lab.py"], ["tradelab_*.html"]),
    (["python", "weekly_recap.py", "--dry-run", "--no-refresh"], ["recap_week_*.html"]),
]


def _normalize(text):
    # Trade Lab bakes a per-render build timestamp into DATA.
    return re.sub(r'"builtAt":\s*"[^"]*"', '"builtAt":"X"', text)


def _render():
    env = {**os.environ, "PYTHONHASHSEED": "0"}
    written = []
    for cmd, patterns in RENDERS:
        # Track by mtime: only files this render actually (re)wrote count.
        before = {p: p.stat().st_mtime for p in PREVIEWS.glob("*.html")}
        r = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True)
        if r.returncode != 0:
            print(f"RENDER FAILED: {' '.join(cmd)}\n{r.stdout[-2000:]}\n{r.stderr[-2000:]}")
            sys.exit(1)
        for pat in patterns:
            for p in PREVIEWS.glob(pat):
                if p not in before or p.stat().st_mtime > before[p]:
                    written.append(p)
    return sorted(set(written))


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode not in ("baseline", "check"):
        print(__doc__)
        sys.exit(2)

    files = _render()
    if not files:
        print("No preview files were written — is data/snapshot.json present?")
        sys.exit(1)

    if mode == "baseline":
        BASELINE.mkdir(exist_ok=True)
        for old in BASELINE.glob("*.html"):
            old.unlink()
        for p in files:
            (BASELINE / p.name).write_bytes(p.read_bytes())
        print(f"Baseline saved: {len(files)} files -> {BASELINE}")
        return

    failures = 0
    for p in files:
        base = BASELINE / p.name
        if not base.exists():
            print(f"[NEW]  {p.name} (no baseline copy — re-run baseline mode if intended)")
            failures += 1
            continue
        old = _normalize(base.read_text(encoding="utf-8"))
        new = _normalize(p.read_text(encoding="utf-8"))
        if old == new:
            print(f"[OK]   {p.name}")
        else:
            failures += 1
            j = next((i for i in range(min(len(old), len(new))) if old[i] != new[i]),
                     min(len(old), len(new)))
            print(f"[DIFF] {p.name} — first difference at char {j}:")
            print(f"       baseline: ...{old[max(0, j - 60):j + 90]}...")
            print(f"       current:  ...{new[max(0, j - 60):j + 90]}...")
    print(f"\n{len(files) - failures}/{len(files)} identical")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
