"""Narrative I/O, jailed to var/narratives/<work_item>/ (DESIGN.md §9).
Agents cannot roam the filesystem — by construction, not by prompt."""
from pathlib import Path


class JailError(Exception):
    pass


class Narratives:
    def __init__(self, base):
        self.base = Path(base).resolve()
        self.base.mkdir(parents=True, exist_ok=True)

    def _path(self, work_item: str, name: str) -> Path:
        if not name.endswith(".md"):
            name += ".md"
        p = (self.base / work_item / name).resolve()
        if self.base not in p.parents:
            raise JailError(f"path escapes the narratives jail: {work_item}/{name}")
        return p

    def write(self, work_item: str, name: str, text: str) -> str:
        p = self._path(work_item, name)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return str(p.relative_to(self.base.parent.parent))  # repo-relative-ish

    def read(self, work_item: str, name: str) -> str:
        return self._path(work_item, name).read_text()

    def list(self, work_item: str) -> list[str]:
        d = (self.base / work_item)
        return sorted(f.name for f in d.glob("*.md")) if d.is_dir() else []
