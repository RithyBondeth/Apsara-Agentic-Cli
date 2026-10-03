"""Discover skill metadata cheaply and read instructions/resources on demand.

Skills are documents, not executable plugins. Reading one grants no permissions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

MAX_SKILL_BYTES = 48_000
MAX_METADATA_BYTES = 4_096
MAX_RESOURCE_BYTES = 200_000
MAX_SKILLS = 100
_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    source: str
    directory: Path

    def metadata(self) -> dict[str, str]:
        return {"name": self.name, "description": self.description, "source": self.source}


def _safe_file(root: Path, relative: str) -> Path:
    candidate = root / relative
    if Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise ValueError("Skill paths must be relative and cannot contain '..'.")
    # Reject links even when their current target is inside the skill directory.
    parts = [candidate]
    while parts[-1] != root:
        parts.append(parts[-1].parent)
    if any(part.is_symlink() for part in parts):
        raise ValueError("Skill files cannot use symlinks.")
    candidate.resolve().relative_to(root.resolve())
    if not candidate.is_file():
        raise ValueError("Skill file does not exist.")
    return candidate


def _read(path: Path, limit: int) -> str:
    with path.open("rb") as stream:
        content = stream.read(limit + 1)
    if len(content) > limit:
        raise ValueError(f"Skill file exceeds {limit} bytes.")
    return content.decode("utf-8")


def _metadata(text: str, fallback: str) -> tuple[str, str]:
    """Support flat name/description frontmatter, including folded descriptions."""
    lines = text.splitlines()
    values: dict[str, str] = {}
    if lines and lines[0].strip() == "---":
        end = next((index for index, line in enumerate(lines[1:], 1) if line.strip() == "---"), None)
        if end is None:
            raise ValueError("Unclosed skill frontmatter.")
        key = None
        for line in lines[1:end]:
            if line[:1].isspace() and key:
                values[key] += " " + line.strip()
                continue
            match = re.match(r"^(name|description):\s*(.*)$", line)
            key = match[1] if match else None
            if match:
                value = match[2].strip()
                values[key] = "" if value in {">", "|", ">-", "|-"} else value.strip("\"'")
        body = lines[end + 1:]
    else:
        body = lines
    name = values.get("name", fallback)
    if not _NAME.fullmatch(name):
        raise ValueError("Skill name must use letters, numbers, underscores, or dashes.")
    description = values.get("description") or next(
        (line.strip() for line in body if line.strip() and not line.lstrip().startswith("#")),
        f"Instructions for {name}",
    )
    return name, " ".join(description.split())[:240]


def discover_skills(workspace: Path, *, user_root: Path | None = None) -> list[Skill]:
    """Project definitions override user definitions, which override bundled ones."""
    roots = [
        ("bundled", Path(__file__).parent / "bundled_skills"),
        ("user", user_root if user_root is not None else Path.home() / ".apsara" / "skills"),
        ("project", workspace.resolve() / ".apsara" / "skills"),
    ]
    found: dict[str, Skill] = {}
    for source, root in roots:
        if not root.is_dir() or root.is_symlink() or root.parent.is_symlink():
            continue
        if source == "project":
            try:
                root.resolve().relative_to(workspace.resolve())
            except ValueError:
                continue
        try:
            directories = sorted(root.iterdir())[:MAX_SKILLS]
        except OSError:
            continue
        for directory in directories:
            try:
                path = _safe_file(directory, "SKILL.md")
                if path.stat().st_size > MAX_SKILL_BYTES:
                    continue
                with path.open("rb") as stream:
                    text = stream.read(MAX_METADATA_BYTES).decode("utf-8", errors="replace")
                name, description = _metadata(text, directory.name)
                found[name] = Skill(name, description, source, directory)
            except (OSError, ValueError, UnicodeError):
                continue
    return [found[name] for name in sorted(found)][:MAX_SKILLS]


def read_skill_file(skill: Skill, resource: str = "SKILL.md") -> str:
    limit = MAX_SKILL_BYTES if resource == "SKILL.md" else MAX_RESOURCE_BYTES
    return _read(_safe_file(skill.directory, resource), limit)
