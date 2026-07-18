"""Shared player-name normalization (stdlib-only leaf module).

One canonical `_name_key` used by every script that matches player names across
sources (fetch_data's FantasyPros<->ESPN merge, send_digest's roster/trade/badge
lookups, weekly_recap's recap lookups). Previously three subtly-different copies
lived in fetch_data / send_digest (`_badge_name_key`) / weekly_recap and could
disagree on names with periods, apostrophes, or hyphens; this is the merged-on
canonical (fetch_data's, the most defensive — keeps >=2 tokens so it never
collapses a real name).
"""
import unicodedata

_NAME_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}


def _name_key(name):
    """Normalized join key for roster matching: accent-stripped, lowercased, and with
    trailing generational suffixes (Jr./Sr./II/III/…) and punctuation removed. Lets
    FantasyPros 'Luis Garcia' match ESPN 'Luis García Jr.' without a per-player patch.
    Keeps at least the first+last token so it never collapses a real name."""
    if not isinstance(name, str):
        return ""
    s = "".join(c for c in unicodedata.normalize("NFD", name) if unicodedata.category(c) != "Mn")
    toks = s.lower().replace(".", " ").replace(",", " ").split()
    while len(toks) > 2 and toks[-1] in _NAME_SUFFIXES:
        toks.pop()
    return " ".join(toks)
