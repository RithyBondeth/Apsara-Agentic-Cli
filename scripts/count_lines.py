"""Quick line/file counts for src and tests (dev utility, not shipped)."""

import pathlib


def count(root: str) -> tuple[int, int]:
    files = [p for p in pathlib.Path(root).rglob("*.py") if "__pycache__" not in str(p)]
    lines = sum(len(p.read_text().splitlines()) for p in files)
    return len(files), lines


for root in ("src", "tests"):
    file_count, line_count = count(root)
    print(f"{root}: {file_count} files, {line_count} lines")
