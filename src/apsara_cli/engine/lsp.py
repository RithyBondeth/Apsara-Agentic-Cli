"""Small on-demand Language Server Protocol client with graceful fallback."""

from __future__ import annotations

import json
import queue
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import url2pathname


SERVER_COMMANDS: dict[str, tuple[tuple[str, ...], str]] = {
    ".py": (("pyright-langserver", "--stdio"), "python"),
    ".js": (("typescript-language-server", "--stdio"), "javascript"),
    ".jsx": (("typescript-language-server", "--stdio"), "javascriptreact"),
    ".ts": (("typescript-language-server", "--stdio"), "typescript"),
    ".tsx": (("typescript-language-server", "--stdio"), "typescriptreact"),
    ".go": (("gopls",), "go"),
    ".rs": (("rust-analyzer",), "rust"),
    ".c": (("clangd",), "c"),
    ".cpp": (("clangd",), "cpp"),
}


def server_for_path(path: Path) -> tuple[list[str], str] | None:
    configured = SERVER_COMMANDS.get(path.suffix.lower())
    if configured is None or shutil.which(configured[0][0]) is None:
        return None
    return list(configured[0]), configured[1]


def capabilities() -> dict[str, Any]:
    available = {
        suffix: command[0][0]
        for suffix, command in SERVER_COMMANDS.items()
        if shutil.which(command[0][0]) is not None
    }
    return {"available": available, "supported_extensions": sorted(SERVER_COMMANDS)}


def _send(stream: Any, payload: dict[str, Any]) -> None:
    body = json.dumps(payload).encode("utf-8")
    stream.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body)
    stream.flush()


def _read(stream: Any) -> dict[str, Any]:
    length = 0
    while True:
        line = stream.readline()
        if not line:
            raise EOFError("language server closed its output")
        if line in {b"\r\n", b"\n"}:
            break
        name, _, value = line.decode("ascii", errors="replace").partition(":")
        if name.lower() == "content-length":
            length = int(value.strip())
    if length <= 0:
        raise ValueError("language server response omitted Content-Length")
    return json.loads(stream.read(length).decode("utf-8"))


def _read_with_timeout(stream: Any, timeout: float) -> dict[str, Any]:
    result: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)

    def worker() -> None:
        try:
            result.put((True, _read(stream)))
        except Exception as exc:  # pragma: no cover - depends on external server
            result.put((False, exc))

    threading.Thread(target=worker, daemon=True).start()
    try:
        ok, value = result.get(timeout=max(0.1, timeout))
    except queue.Empty as exc:
        raise TimeoutError("language server response timed out") from exc
    if not ok:
        raise value
    return value


def _request(
    process: subprocess.Popen[bytes], request_id: int, method: str, params: dict[str, Any], timeout: int
) -> Any:
    assert process.stdin is not None and process.stdout is not None
    _send(process.stdin, {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
    deadline = time.monotonic() + max(1, timeout)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("language server request timed out")
        message = _read_with_timeout(process.stdout, remaining)
        if message.get("id") != request_id:
            continue
        if message.get("error"):
            raise RuntimeError(str(message["error"]))
        return message.get("result")


def _location_lines(result: Any, workspace: Path) -> list[str]:
    values = result if isinstance(result, list) else [result] if isinstance(result, dict) else []
    lines: list[str] = []
    for item in values:
        if not isinstance(item, dict):
            continue
        uri = item.get("uri") or item.get("targetUri")
        span = item.get("range") or item.get("targetSelectionRange") or {}
        start = span.get("start", {}) if isinstance(span, dict) else {}
        if not isinstance(uri, str):
            continue
        parsed = urlparse(uri)
        if parsed.scheme != "file":
            continue
        uri_path = url2pathname(parsed.path)
        if parsed.netloc and parsed.netloc != "localhost":
            uri_path = f"//{parsed.netloc}{uri_path}"
        candidate = Path(uri_path).resolve()
        try:
            relative = candidate.relative_to(workspace)
        except ValueError:
            continue
        lines.append(
            f"{relative}:{int(start.get('line', 0)) + 1}:{int(start.get('character', 0)) + 1} (lsp)"
        )
    return lines


def query_locations(
    workspace: Path,
    path: Path,
    *,
    line: int,
    column: int,
    operation: str,
    timeout: int = 20,
) -> list[str]:
    workspace = workspace.resolve()
    path = path.resolve()
    path.relative_to(workspace)
    server = server_for_path(path)
    if server is None:
        raise RuntimeError(f"No installed language server supports {path.suffix or 'this file'}")
    command, language_id = server
    process = subprocess.Popen(
        command,
        cwd=workspace,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    try:
        root_uri = workspace.as_uri()
        _request(process, 1, "initialize", {
            "processId": None,
            "rootUri": root_uri,
            "capabilities": {},
            "workspaceFolders": [{"uri": root_uri, "name": workspace.name}],
        }, timeout)
        assert process.stdin is not None
        _send(process.stdin, {"jsonrpc": "2.0", "method": "initialized", "params": {}})
        _send(process.stdin, {
            "jsonrpc": "2.0",
            "method": "textDocument/didOpen",
            "params": {"textDocument": {
                "uri": path.as_uri(),
                "languageId": language_id,
                "version": 1,
                "text": path.read_text(encoding="utf-8", errors="replace"),
            }},
        })
        method = "textDocument/definition" if operation == "definition" else "textDocument/references"
        params: dict[str, Any] = {
            "textDocument": {"uri": path.as_uri()},
            "position": {"line": max(0, line - 1), "character": max(0, column - 1)},
        }
        if operation == "references":
            params["context"] = {"includeDeclaration": True}
        result = _request(process, 2, method, params, timeout)
        return _location_lines(result, workspace)
    finally:
        try:
            _request(process, 3, "shutdown", {}, 3)
            if process.stdin is not None:
                _send(process.stdin, {"jsonrpc": "2.0", "method": "exit", "params": {}})
        except Exception:
            pass
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
