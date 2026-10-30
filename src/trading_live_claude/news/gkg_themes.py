"""Frozen map from GDELT GKG theme codes to the five WorldMonitor stress domains.

The graph adapter's bridge (user choice 2026-10-30: GDELT's own theme codes, not a keyword map). The map
lives in ``config/gkg_theme_domains.yaml``; its ``id`` is a hash of the loaded content, so a change to the
map is visible on every record built under it and old records are never silently re-read under a new map.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class ThemeMap:
    id: str
    exact: Mapping[str, frozenset[str]]
    prefix: Mapping[str, tuple[str, ...]]
    exclude: frozenset[str]

    @property
    def domains(self) -> tuple[str, ...]:
        return tuple(sorted(self.exact))

    def domains_for(self, themes: Iterable[str]) -> frozenset[str]:
        """Domains an article belongs to, from its theme codes. Unmapped codes contribute nothing."""
        hit: set[str] = set()
        for code in themes:
            if code in self.exclude:
                continue
            for d in self.domains:
                if code in self.exact[d] or code.startswith(self.prefix[d]):
                    hit.add(d)
        return frozenset(hit)


def load_theme_map(path: Path) -> ThemeMap:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    doms = raw.get("domains") or {}
    exact = {d: frozenset(str(c) for c in (spec or {}).get("exact") or ()) for d, spec in doms.items()}
    prefix = {d: tuple(str(c) for c in (spec or {}).get("prefix") or ()) for d, spec in doms.items()}
    exclude = frozenset(str(c) for c in raw.get("exclude") or ())
    canon = {"domains": {d: {"exact": sorted(exact[d]), "prefix": sorted(prefix[d])} for d in sorted(doms)},
             "exclude": sorted(exclude)}
    mid = hashlib.sha256(json.dumps(canon, sort_keys=True).encode()).hexdigest()[:12]
    return ThemeMap(id=mid, exact=exact, prefix=prefix, exclude=exclude)
