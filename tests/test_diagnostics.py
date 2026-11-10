import json
import os
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from apsara_cli.engine.diagnostics import create_diagnostic_bundle


def _options(workspace):
    return SimpleNamespace(
        workspace_root=workspace,
        session="private-customer-name",
        stateless=False,
        allow_bash=True,
        allowed_commands={"pytest", "git"},
        max_file_size=1_000_000,
        auto_approve=False,
        use_color=True,
        dry_run=False,
        read_only=False,
        bash_timeout=30,
    )


def _history(secret, source):
    return [
        {"role": "user", "content": f"OPENAI_API_KEY={secret}\n{source}"},
        {
            "role": "assistant",
            "content": "I will inspect it.",
            "tool_calls": [{
                "id": "call-1",
                "type": "function",
                "function": {
                    "name": "write_to_file",
                    "arguments": json.dumps({"path": "app.py", "content": source}),
                },
            }],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": source},
    ]


def test_default_bundle_omits_conversation_tool_payloads_and_workspace_identity(tmp_path):
    secret = "sk-super-secret-value-123456"
    source = "def proprietary_customer_algorithm(): return 42"
    log_file = tmp_path / "source.log"
    log_file.write_text(
        "--- Session started ---\n"
        f"unstructured payload: {source}\n"
        "[12:00:00] [TOOL_CALL] write_to_file\n"
        f"  | {{\"content\": \"{source}\", \"api_key\": \"{secret}\"}}\n"
        "[12:00:01] [TOOL_RESULT] Output received\n"
        f"  | {source}\n",
        encoding="utf-8",
    )

    bundle = create_diagnostic_bundle(
        tmp_path,
        "test/model",
        _history(secret, source),
        _options(tmp_path),
        log_file,
        generated_at=datetime(2026, 8, 9, tzinfo=timezone.utc),
    )

    persisted = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(bundle.iterdir())
    )
    state = json.loads((bundle / "session_state.json").read_text(encoding="utf-8"))
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))

    assert secret not in persisted
    assert source not in persisted
    assert "private-customer-name" not in persisted
    assert str(tmp_path) not in persisted
    assert "[OMITTED" in persisted
    assert state["privacy_mode"] == "metadata-only"
    assert state["history"][1]["tool_calls"][0]["function"]["name"] == "write_to_file"
    assert manifest["message_count"] == 3
    assert manifest["message_roles"] == {"assistant": 1, "tool": 1, "user": 1}
    assert manifest["log"]["included"] is True


def test_content_opt_in_preserves_source_but_still_redacts_credentials(tmp_path):
    secret = "gsk_supersecretvalue123456"
    source = "def useful_reproduction(): return 'customer-specific'"

    bundle = create_diagnostic_bundle(
        tmp_path,
        "test/model",
        _history(secret, source),
        _options(tmp_path),
        None,
        include_content=True,
        generated_at=datetime(2026, 8, 9, 1, tzinfo=timezone.utc),
    )

    persisted = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(bundle.iterdir())
    )
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))

    assert source in persisted
    assert secret not in persisted
    assert "[REDACTED]" in persisted
    assert manifest["privacy_mode"] == "content-included"
    assert "after confirmation" in manifest["content_notice"]


def test_bundle_bounds_log_size_and_reports_omitted_bytes(tmp_path):
    log_file = tmp_path / "large.log"
    log_file.write_text("old detail\n" * 30_000, encoding="utf-8")

    bundle = create_diagnostic_bundle(
        tmp_path,
        "test/model",
        [],
        _options(tmp_path),
        log_file,
        generated_at=datetime(2026, 8, 9, 2, tzinfo=timezone.utc),
    )

    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["log"]["omitted_leading_bytes"] > 0
    assert (bundle / "session.log").stat().st_size <= 200_100


def test_bundle_refuses_to_follow_log_symlink(tmp_path):
    target = tmp_path / "outside.log"
    target.write_text("secret log", encoding="utf-8")
    link = tmp_path / "linked.log"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable on this platform")

    bundle = create_diagnostic_bundle(
        tmp_path,
        "test/model",
        [],
        _options(tmp_path),
        link,
        generated_at=datetime(2026, 8, 9, 3, tzinfo=timezone.utc),
    )

    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["log"]["included"] is False
    assert not (bundle / "session.log").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits are not portable")
def test_bundle_files_are_private_on_posix(tmp_path):
    bundle = create_diagnostic_bundle(
        tmp_path,
        "test/model",
        [],
        _options(tmp_path),
        None,
        generated_at=datetime(2026, 8, 9, 4, tzinfo=timezone.utc),
    )

    assert bundle.stat().st_mode & 0o777 == 0o700
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in bundle.iterdir())
