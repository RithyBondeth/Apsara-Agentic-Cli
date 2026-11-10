"""Semantic repository intelligence with dependency-free fallbacks.

Python uses the standard-library AST. Other languages use Tree-sitter when the
optional ``intelligence`` extra is installed, then fall back to conservative
definition patterns. Normal file edits only run cheap syntax diagnostics;
project compiler checks are always explicit.
"""

from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

IGNORED = {".git", ".apsara", ".venv", "venv", "node_modules", "dist", "build", "__pycache__"}
LANGUAGES = {
    ".py": "Python", ".js": "JavaScript", ".jsx": "JavaScript", ".ts": "TypeScript",
    ".tsx": "TypeScript", ".go": "Go", ".rs": "Rust", ".java": "Java",
    ".rb": "Ruby", ".php": "PHP", ".cs": "C#", ".cpp": "C++", ".c": "C",
}
PARSER_NAMES = {
    ".py": "python", ".js": "javascript", ".jsx": "javascript", ".ts": "typescript",
    ".tsx": "tsx", ".go": "go", ".rs": "rust", ".java": "java", ".rb": "ruby",
    ".php": "php", ".cs": "c_sharp", ".cpp": "cpp", ".c": "c",
}
SYMBOL_PATTERNS = (
    ("definition", re.compile(r"^\s*(?:async\s+)?(def|class)\s+([A-Za-z_]\w*)")),
    ("definition", re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?(function|class|interface|type|enum)\s+([A-Za-z_$][\w$]*)")),
    ("definition", re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(fn|struct|enum|trait|type)\s+([A-Za-z_]\w*)")),
    ("definition", re.compile(r"^\s*(func|type)\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)")),
)
TREE_SYMBOL_NODES = {
    "function_definition", "class_definition", "function_declaration", "class_declaration",
    "interface_declaration", "type_alias_declaration", "enum_declaration", "method_definition",
    "method_declaration", "function_item", "struct_item", "enum_item", "trait_item",
    "type_item", "type_declaration", "method_declaration",
}
IDENTIFIER_NODES = {"identifier", "type_identifier", "field_identifier", "property_identifier"}


@dataclass(frozen=True)
class Symbol:
    name: str
    kind: str
    path: Path
    start_line: int
    end_line: int
    start_column: int = 0
    provider: str = "pattern"
    name_line: int = 0

    def location(self, workspace: Path) -> str:
        try:
            path = self.path.relative_to(workspace)
        except ValueError:
            path = self.path
        return f"{path}:{self.name_line or self.start_line}:{self.start_column + 1}"


@dataclass(frozen=True)
class Reference:
    name: str
    path: Path
    line: int
    column: int
    is_definition: bool = False
    provider: str = "text"


@dataclass(frozen=True)
class Diagnostic:
    path: Path
    line: int
    column: int
    severity: str
    message: str
    source: str


def source_files(workspace: Path) -> Iterator[Path]:
    workspace = workspace.resolve()
    for path in workspace.rglob("*"):
        if path.is_symlink() or not path.is_file() or path.suffix.lower() not in LANGUAGES:
            continue
        if any(part in IGNORED for part in path.relative_to(workspace).parts):
            continue
        try:
            path.resolve().relative_to(workspace)
        except ValueError:
            continue
        yield path


def _python_symbols(path: Path, content: str) -> list[Symbol]:
    tree = ast.parse(content, filename=str(path))
    lines = content.splitlines()
    result: list[Symbol] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            kind = "class" if isinstance(node, ast.ClassDef) else "function"
            start = min([node.lineno] + [item.lineno for item in getattr(node, "decorator_list", [])])
            name_column = lines[node.lineno - 1].find(node.name, node.col_offset)
            result.append(Symbol(node.name, kind, path, start, getattr(node, "end_lineno", node.lineno), max(node.col_offset, name_column), "python-ast", node.lineno))
    return sorted(result, key=lambda item: (item.start_line, item.start_column))


def _tree_parser(path: Path):
    parser_name = PARSER_NAMES.get(path.suffix.lower())
    if not parser_name:
        return None
    try:
        from tree_sitter_language_pack import get_parser
        return get_parser(parser_name)
    except (ImportError, LookupError, OSError, RuntimeError):
        return None


def _walk_tree(node) -> Iterator:
    yield node
    for child in node.children:
        yield from _walk_tree(child)


def _tree_symbols(path: Path, content: str) -> list[Symbol] | None:
    parser = _tree_parser(path)
    if parser is None:
        return None
    encoded = content.encode("utf-8")
    root = parser.parse(encoded).root_node
    result: list[Symbol] = []
    for node in _walk_tree(root):
        if node.type not in TREE_SYMBOL_NODES:
            continue
        name_node = node.child_by_field_name("name")
        if name_node is None:
            name_node = next((child for child in node.children if child.type in IDENTIFIER_NODES), None)
        if name_node is None:
            continue
        name = encoded[name_node.start_byte:name_node.end_byte].decode("utf-8", errors="replace")
        kind = node.type.replace("_declaration", "").replace("_definition", "").replace("_item", "").replace("_", " ")
        result.append(Symbol(name, kind, path, node.start_point[0] + 1, node.end_point[0] + 1, name_node.start_point[1], "tree-sitter"))
    return result


def _pattern_symbols(path: Path, content: str) -> list[Symbol]:
    result: list[Symbol] = []
    for number, line in enumerate(content.splitlines(), 1):
        for _, pattern in SYMBOL_PATTERNS:
            match = pattern.match(line)
            if match:
                result.append(Symbol(match.group(2), match.group(1), path, number, number, match.start(2), "pattern"))
                break
    return result


def symbols_in_file(path: Path) -> list[Symbol]:
    content = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix.lower() == ".py":
        try:
            return _python_symbols(path, content)
        except SyntaxError:
            return _pattern_symbols(path, content)
    return _tree_symbols(path, content) or _pattern_symbols(path, content)


def definitions(workspace: Path, name: str, path_hint: str = "", limit: int = 100) -> list[Symbol]:
    exact: list[Symbol] = []
    partial: list[Symbol] = []
    hint = path_hint.lower()
    for path in source_files(workspace):
        if hint and hint not in str(path.relative_to(workspace)).lower():
            continue
        try:
            symbols = symbols_in_file(path)
        except OSError:
            continue
        for symbol in symbols:
            if symbol.name == name:
                exact.append(symbol)
            elif name.lower() in symbol.name.lower():
                partial.append(symbol)
            if len(exact) + len(partial) >= limit:
                break
    return (exact or partial)[:limit]


def references(workspace: Path, name: str, path_filter: str = "", limit: int = 200) -> list[Reference]:
    result: list[Reference] = []
    token = re.compile(rf"\b{re.escape(name)}\b")
    for path in source_files(workspace):
        relative = str(path.relative_to(workspace))
        if path_filter and path_filter.lower() not in relative.lower():
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
            known = {((item.name_line or item.start_line), item.start_column) for item in symbols_in_file(path) if item.name == name}
        except OSError:
            continue
        if path.suffix.lower() == ".py":
            try:
                tree = ast.parse(content, filename=str(path))
                positions = set(known)
                for node in ast.walk(tree):
                    if isinstance(node, ast.Name) and node.id == name:
                        positions.add((node.lineno, node.col_offset))
                    elif isinstance(node, ast.Attribute) and node.attr == name:
                        positions.add((node.end_lineno or node.lineno, (node.end_col_offset or 0) - len(name)))
                for line_number, column in sorted(positions):
                    result.append(Reference(name, path, line_number, column, (line_number, column) in known, "python-ast"))
                    if len(result) >= limit:
                        return result
                continue
            except SyntaxError:
                pass
        parser = _tree_parser(path)
        if parser is not None:
            encoded = content.encode("utf-8")
            for node in _walk_tree(parser.parse(encoded).root_node):
                if node.type not in IDENTIFIER_NODES:
                    continue
                value = encoded[node.start_byte:node.end_byte].decode("utf-8", errors="replace")
                position = (node.start_point[0] + 1, node.start_point[1])
                if value == name:
                    result.append(Reference(name, path, position[0], position[1], position in known, "tree-sitter"))
                    if len(result) >= limit:
                        return result
            continue
        provider = "text"
        for line_number, line in enumerate(content.splitlines(), 1):
            for match in token.finditer(line):
                result.append(Reference(name, path, line_number, match.start(), (line_number, match.start()) in known, provider))
                if len(result) >= limit:
                    return result
    return result


def syntax_diagnostics(path: Path) -> list[Diagnostic]:
    """Return cheap, side-effect-free parser diagnostics for one source file."""
    if not path.exists() or path.suffix.lower() not in LANGUAGES:
        return []
    content = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix.lower() == ".py":
        try:
            ast.parse(content, filename=str(path))
        except SyntaxError as exc:
            return [Diagnostic(path, exc.lineno or 1, exc.offset or 1, "error", exc.msg, "python-ast")]
        return []
    parser = _tree_parser(path)
    if parser is None:
        return []
    tree = parser.parse(content.encode("utf-8"))
    issues: list[Diagnostic] = []
    for node in _walk_tree(tree.root_node):
        if node.type == "ERROR" or node.is_missing:
            issues.append(Diagnostic(path, node.start_point[0] + 1, node.start_point[1] + 1, "error", "Syntax error" if node.type == "ERROR" else f"Missing {node.type}", "tree-sitter"))
            if len(issues) >= 50:
                break
    return issues


def _run_diagnostic(command: list[str], workspace: Path, source: str, timeout: int = 120) -> list[Diagnostic]:
    try:
        completed = subprocess.run(command, cwd=str(workspace), capture_output=True, text=True, timeout=timeout)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return [Diagnostic(workspace, 1, 1, "warning", str(exc), source)]
    if completed.returncode == 0:
        return []
    output = (completed.stdout + "\n" + completed.stderr).strip()
    return [Diagnostic(workspace, 1, 1, "error", output[-8000:] or f"{source} exited {completed.returncode}", source)]


def project_diagnostics(workspace: Path) -> tuple[str, list[Diagnostic]]:
    """Run the project's native type/compiler checker without a shell."""
    if (workspace / "pyproject.toml").exists() and shutil.which("pyright"):
        try:
            completed = subprocess.run(["pyright", "--outputjson"], cwd=str(workspace), capture_output=True, text=True, timeout=120)
            payload = json.loads(completed.stdout or "{}")
            issues = [Diagnostic(Path(item.get("file", workspace)), int(item.get("range", {}).get("start", {}).get("line", 0)) + 1, int(item.get("range", {}).get("start", {}).get("character", 0)) + 1, item.get("severity", "error"), item.get("message", "Pyright diagnostic"), "pyright") for item in payload.get("generalDiagnostics", [])]
            return "pyright", issues
        except (json.JSONDecodeError, subprocess.TimeoutExpired):
            pass
    if (workspace / "tsconfig.json").exists() and shutil.which("tsc"):
        return "tsc", _run_diagnostic(["tsc", "--noEmit", "--pretty", "false"], workspace, "tsc")
    if (workspace / "go.mod").exists() and shutil.which("go"):
        return "go test", _run_diagnostic(["go", "test", "./..."], workspace, "go test")
    if (workspace / "Cargo.toml").exists() and shutil.which("cargo"):
        return "cargo check", _run_diagnostic(["cargo", "check", "--message-format", "short"], workspace, "cargo check")
    return "none", []


def capabilities() -> dict[str, object]:
    try:
        import tree_sitter_language_pack  # noqa: F401
        tree_sitter = True
    except ImportError:
        tree_sitter = False
    from apsara_cli.engine.lsp import capabilities as lsp_capabilities
    return {
        "python_ast": True,
        "tree_sitter": tree_sitter,
        "project_checkers": [name for name in ("pyright", "tsc", "go", "cargo") if shutil.which(name)],
        "lsp": lsp_capabilities(),
    }


def format_diagnostics(workspace: Path, issues: Iterable[Diagnostic]) -> str:
    rendered = []
    for issue in issues:
        try:
            path = issue.path.relative_to(workspace)
        except ValueError:
            path = issue.path
        rendered.append(f"{path}:{issue.line}:{issue.column}: {issue.severity}: {issue.message} [{issue.source}]")
    return "\n".join(rendered)


def repository_map(workspace: Path, max_files: int = 200) -> str:
    files = list(source_files(workspace))[:max_files]
    languages = Counter(LANGUAGES[path.suffix.lower()] for path in files)
    manifests = [name for name in ("pyproject.toml", "package.json", "Cargo.toml", "go.mod", "pom.xml", "requirements.txt") if (workspace / name).exists()]
    lines = [f"Repository: {workspace.name}", f"Languages: {', '.join(f'{key} {value}' for key, value in languages.most_common()) or 'unknown'}"]
    if manifests:
        lines.append(f"Manifests: {', '.join(manifests)}")
    for path in files:
        try:
            symbols = [symbol.name for symbol in symbols_in_file(path)]
        except (OSError, SyntaxError):
            symbols = []
        suffix = f" — {', '.join(symbols[:8])}" if symbols else ""
        lines.append(f"- {path.relative_to(workspace)}{suffix}")
    if len(files) == max_files:
        lines.append(f"… limited to {max_files} source files")
    return "\n".join(lines)


def find_symbol(workspace: Path, query: str, limit: int = 100) -> str:
    result = definitions(workspace, query, limit=limit)
    if not result:
        return f"No symbols matching '{query}'."
    return "\n".join(
        f"{item.path.relative_to(workspace)}:{item.name_line or item.start_line}: {item.name} ({item.kind}, {item.provider})"
        for item in result
    )
