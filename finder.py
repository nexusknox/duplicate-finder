"""Core logic for Duplicate Finder.

No UI code lives here, so everything can be tested on temporary folders.
"""

import hashlib
import os
import stat
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

QUICK_HASH_BYTES = 64 * 1024   # first 64 KB: a cheap first check
READ_CHUNK = 1024 * 1024       # read big files 1 MB at a time

# If a folder contains any of these, it's a code project. Skip it.
PROJECT_MARKERS = {
    ".git", "package.json", "requirements.txt", "pyproject.toml",
    ".venv", "venv", "node_modules", "setup.py", "Cargo.toml", "pom.xml",
}
SKIP_NAMES = {"desktop.ini", "thumbs.db"}

# Windows attribute flags. Files with these may be links, or OneDrive
# "online-only" placeholders: reading them would download them, so skip.
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
FILE_ATTRIBUTE_OFFLINE = 0x1000
FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x40000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000
SKIP_FLAGS = (
    stat.FILE_ATTRIBUTE_HIDDEN
    | stat.FILE_ATTRIBUTE_SYSTEM
    | FILE_ATTRIBUTE_REPARSE_POINT
    | FILE_ATTRIBUTE_OFFLINE
    | FILE_ATTRIBUTE_RECALL_ON_OPEN
    | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
)


# ---------------------------------------------------------------------------
# Step 1: list every file worth checking
# ---------------------------------------------------------------------------

@dataclass
class ScanResult:
    files: list[Path] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)  # friendly messages


def is_project_folder(folder: Path) -> bool:
    return any((folder / marker).exists() for marker in PROJECT_MARKERS)


def should_skip(entry: os.DirEntry) -> bool:
    """Hidden, system, link or cloud-placeholder items are never touched.

    follow_symlinks=False looks at the item itself, not what it points to.
    """
    if entry.name.startswith(".") or entry.name.lower() in SKIP_NAMES:
        return True
    if entry.is_symlink():
        return True
    attributes = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
    return bool(attributes & SKIP_FLAGS)


def list_files(folders: list[Path], min_size: int = 1) -> ScanResult:
    """Every file of at least `min_size` bytes inside `folders`, subfolders included.

    Empty files are skipped by default: they're all "identical", which is
    true but useless. Overlapping folders (Documents and Documents\\Work) are
    only counted once, otherwise a file would look like a copy of itself.
    """
    result = ScanResult()
    seen: set[Path] = set()
    to_visit = [Path(f) for f in folders]

    while to_visit:
        folder = to_visit.pop()
        real = folder.resolve()
        if real in seen or is_project_folder(folder):
            continue
        seen.add(real)

        try:
            entries = sorted(os.scandir(folder), key=lambda e: e.name)
        except OSError:
            result.problems.append(f"{folder.name}: couldn't open this folder (no permission?)")
            continue

        for entry in entries:
            try:
                if should_skip(entry):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    to_visit.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    if entry.stat(follow_symlinks=False).st_size >= min_size:
                        result.files.append(Path(entry.path))
            except OSError:
                result.problems.append(f"{entry.name}: couldn't read it")

    result.files = sorted(set(p.resolve() for p in result.files))
    return result


# ---------------------------------------------------------------------------
# Step 2: group files with identical content
# ---------------------------------------------------------------------------

def file_hash(path: Path, limit: int | None = None) -> str:
    """SHA-256 of the file's content (or of only its first `limit` bytes)."""
    digest = hashlib.sha256()
    remaining = limit
    with open(path, "rb") as f:
        while remaining is None or remaining > 0:
            size = READ_CHUNK if remaining is None else min(READ_CHUNK, remaining)
            chunk = f.read(size)
            if not chunk:
                break
            digest.update(chunk)
            if remaining is not None:
                remaining -= len(chunk)
    return digest.hexdigest()


@dataclass
class DuplicateGroup:
    size: int           # size of ONE copy, in bytes
    files: list[Path]   # files[0] is the suggested copy to keep

    @property
    def wasted(self) -> int:
        """Bytes freed by removing every copy except one."""
        return self.size * (len(self.files) - 1)


def keeper_order(path: Path) -> tuple:
    """Sort key: shortest path first, so 'photo.jpg' beats 'photo (1).jpg'
    and 'Pictures\\photo.jpg' beats 'Pictures\\Old\\Backup\\photo.jpg'."""
    return (len(str(path)), str(path).lower())


def find_duplicates(
    files: list[Path],
    hasher: Callable[[Path, int | None], str] = file_hash,
) -> tuple[list[DuplicateGroup], list[str]]:
    """Return groups of identical files, biggest wasted space first.

    Three passes, each cheaper than the next, so most files are never fully read:
      1. size        - files with a unique size can't have a copy
      2. quick hash  - first 64 KB only
      3. full hash   - the whole file, only for files still matching
    `hasher` is swappable so tests can count how often files are read.
    """
    problems: list[str] = []

    def hash_or_none(path: Path, limit: int | None) -> str | None:
        try:
            return hasher(path, limit)
        except OSError:
            problems.append(f"{path.name}: couldn't read it (it may be open in another app)")
            return None

    def split_by(group: list[Path], key) -> list[list[Path]]:
        buckets: dict = defaultdict(list)
        for path in group:
            value = key(path)
            if value is not None:
                buckets[value].append(path)
        return [paths for paths in buckets.values() if len(paths) > 1]

    def size_of(path: Path) -> int | None:
        try:
            return path.stat().st_size
        except OSError:
            problems.append(f"{path.name}: couldn't read it")
            return None

    groups: list[DuplicateGroup] = []
    for same_size in split_by(files, size_of):
        size = same_size[0].stat().st_size
        for same_start in split_by(same_size, lambda p: hash_or_none(p, QUICK_HASH_BYTES)):
            if size <= QUICK_HASH_BYTES:
                matches = [same_start]  # the quick hash already covered the whole file
            else:
                matches = split_by(same_start, lambda p: hash_or_none(p, None))
            for paths in matches:
                groups.append(DuplicateGroup(size=size, files=sorted(paths, key=keeper_order)))

    groups.sort(key=lambda g: (-g.wasted, str(g.files[0]).lower()))
    return groups, problems
