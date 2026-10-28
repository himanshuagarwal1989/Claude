#!/usr/bin/env python3
"""Downloads Defragmenter: sort a messy Downloads folder into category folders.

Runs entirely locally. Nothing is moved unless you pass --apply, and every
applied run writes a log that --undo can reverse.

    python3 defrag.py                      # preview plan for ~/Downloads
    python3 defrag.py --apply              # perform the moves
    python3 defrag.py --undo <log.json>    # put everything back
"""
import argparse
import hashlib
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

CATEGORIES: Dict[str, List[str]] = {
    "Images": ["jpg", "jpeg", "png", "gif", "bmp", "tiff", "tif", "webp", "heic", "svg", "ico", "raw"],
    "Documents": ["pdf", "doc", "docx", "odt", "rtf", "txt", "md", "pages", "epub"],
    "Spreadsheets": ["xls", "xlsx", "xlsm", "csv", "tsv", "ods", "numbers"],
    "Presentations": ["ppt", "pptx", "odp", "key"],
    "Archives": ["zip", "rar", "7z", "tar", "gz", "tgz", "bz2", "xz"],
    "Installers": ["dmg", "pkg", "app", "exe", "msi", "deb", "rpm", "iso"],
    "Audio": ["mp3", "wav", "aac", "flac", "m4a", "ogg", "aiff"],
    "Video": ["mp4", "mov", "avi", "mkv", "webm", "m4v", "wmv"],
    "Code": ["py", "js", "ts", "html", "css", "json", "xml", "yaml", "yml", "sh", "ipynb", "sql", "java", "c", "cpp"],
    "Fonts": ["ttf", "otf", "woff", "woff2"],
}
EXT_TO_CATEGORY = {ext: cat for cat, exts in CATEGORIES.items() for ext in exts}
OTHER = "Other"
DUPLICATES = "_Duplicates"
LOG_DIR_NAME = ".defrag_logs"

# Files still downloading or OS metadata: never touch these.
SKIP_SUFFIXES = {"crdownload", "part", "download", "partial", "tmp"}
SKIP_NAMES = {".DS_Store", ".localized"}
MANAGED_DIRS = set(CATEGORIES) | {OTHER, DUPLICATES, LOG_DIR_NAME}


def classify(path: Path) -> str:
    return EXT_TO_CATEGORY.get(path.suffix.lower().lstrip("."), OTHER)


def file_hash(path: Path, chunk_size: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()


def scan(root: Path) -> List[Path]:
    files = []
    for p in sorted(root.iterdir()):
        if p.name in SKIP_NAMES or p.name in MANAGED_DIRS:
            continue
        if p.suffix.lower().lstrip(".") in SKIP_SUFFIXES:
            continue
        # .app bundles are directories but should be treated as one file.
        if p.is_file() or (p.is_dir() and p.suffix.lower() == ".app"):
            files.append(p)
    return files


def find_duplicates(files: List[Path]) -> Dict[Path, Path]:
    """Map each duplicate file to the original it copies (oldest file wins)."""
    by_size: Dict[int, List[Path]] = {}
    for p in files:
        if p.is_file():
            by_size.setdefault(p.stat().st_size, []).append(p)

    dupes: Dict[Path, Path] = {}
    for group in by_size.values():
        if len(group) < 2:
            continue
        by_hash: Dict[str, List[Path]] = {}
        for p in group:
            by_hash.setdefault(file_hash(p), []).append(p)
        for same in by_hash.values():
            same.sort(key=lambda p: p.stat().st_mtime)
            for copy in same[1:]:
                dupes[copy] = same[0]
    return dupes


def unique_destination(dest: Path, taken: set) -> Path:
    candidate, n = dest, 1
    while candidate.exists() or candidate in taken:
        candidate = dest.with_name(f"{dest.stem} ({n}){dest.suffix}")
        n += 1
    taken.add(candidate)
    return candidate


def plan_moves(root: Path, check_duplicates: bool) -> Tuple[List[Tuple[Path, Path]], Dict[Path, Path]]:
    files = scan(root)
    dupes = find_duplicates(files) if check_duplicates else {}
    taken: set = set()
    moves = []
    for p in files:
        folder = DUPLICATES if p in dupes else classify(p)
        moves.append((p, unique_destination(root / folder / p.name, taken)))
    return moves, dupes


def print_plan(root: Path, moves: List[Tuple[Path, Path]], dupes: Dict[Path, Path]) -> None:
    counts: Dict[str, int] = {}
    for _, dest in moves:
        counts[dest.parent.name] = counts.get(dest.parent.name, 0) + 1
    print(f"\n{len(moves)} items in {root}\n")
    for folder, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {folder:<15} {n:>5}")
    if dupes:
        print(f"\nDuplicates ({len(dupes)}):")
        for copy, orig in sorted(dupes.items()):
            print(f"  {copy.name}  ==  {orig.name}")


def apply_moves(root: Path, moves: List[Tuple[Path, Path]]) -> Path:
    log_dir = root / LOG_DIR_NAME
    log_dir.mkdir(exist_ok=True)
    log_path = log_dir / f"defrag-{datetime.now():%Y%m%d-%H%M%S}.json"
    done = []
    try:
        for src, dest in moves:
            dest.parent.mkdir(exist_ok=True)
            shutil.move(str(src), str(dest))
            done.append({"from": str(src), "to": str(dest)})
    finally:
        # Write the log even on failure so partial runs can still be undone.
        log_path.write_text(json.dumps(done, indent=2))
    return log_path


def undo(log_path: Path) -> None:
    entries = json.loads(log_path.read_text())
    restored = 0
    for entry in reversed(entries):
        src, dest = Path(entry["to"]), Path(entry["from"])
        if not src.exists():
            print(f"  missing, skipped: {src}")
            continue
        if dest.exists():
            print(f"  original path occupied, skipped: {dest}")
            continue
        shutil.move(str(src), str(dest))
        restored += 1
    print(f"Restored {restored}/{len(entries)} items.")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("folder", nargs="?", default=str(Path.home() / "Downloads"))
    parser.add_argument("--apply", action="store_true", help="actually move files (default is a dry run)")
    parser.add_argument("--no-dupes", action="store_true", help="skip duplicate detection (faster)")
    parser.add_argument("--undo", metavar="LOG", help="reverse a previous run using its log file")
    args = parser.parse_args(argv)

    if args.undo:
        undo(Path(args.undo).expanduser())
        return 0

    root = Path(args.folder).expanduser().resolve()
    if not root.is_dir():
        print(f"Not a folder: {root}", file=sys.stderr)
        return 1

    moves, dupes = plan_moves(root, check_duplicates=not args.no_dupes)
    print_plan(root, moves, dupes)

    if not args.apply:
        print("\nDry run only. Re-run with --apply to move files.")
        return 0

    log_path = apply_moves(root, moves)
    print(f"\nMoved {len(moves)} items. Undo with:\n  python3 {Path(__file__).name} --undo \"{log_path}\"")
    return 0


if __name__ == "__main__":
    sys.exit(main())
