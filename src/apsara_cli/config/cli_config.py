import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - tomllib is stdlib only on 3.11+
    import tomli as tomllib


DEFAULT_CONFIG_PATH = Path.home() / ".apsara" / "config.toml"
LOCAL_CONFIG_DIRNAME = ".apsara"
LOCAL_CONFIG_FILENAME = "config.toml"


@dataclass
class CliDefaults:
    workspace: Optional[str] = None
    model: Optional[str] = None
    session: Optional[str] = None
    stateless: Optional[bool] = None
    allow_bash: Optional[bool] = None
    allowed_commands: Optional[list[str]] = None
    max_file_size: Optional[int] = None
    bash_timeout: Optional[int] = None
    auto_approve: Optional[bool] = None
    color: Optional[bool] = None


@dataclass
class CliUi:
    welcome_title: Optional[str] = None
    welcome_subtitle: Optional[str] = None
    powered_by: Optional[str] = None
    welcome_animation: Optional[bool] = None
    welcome_frame_delay_ms: Optional[int] = None
    mascot: Optional[bool] = None
    mascot_animation: Optional[bool] = None


@dataclass
class CliTheme:
    body: Optional[str] = None
    muted: Optional[str] = None
    dim: Optional[str] = None
    heading: Optional[str] = None
    accent: Optional[str] = None
    success: Optional[str] = None
    info_text: Optional[str] = None
    warning_text: Optional[str] = None
    error_text: Optional[str] = None
    blocked_text: Optional[str] = None
    info_bg: Optional[str] = None
    ok_bg: Optional[str] = None
    warn_bg: Optional[str] = None
    error_bg: Optional[str] = None
    status_bg: Optional[str] = None
    blocked_bg: Optional[str] = None
    spinner_bg: Optional[str] = None
    muted_bg: Optional[str] = None
    assistant_label: Optional[str] = None
    user_label: Optional[str] = None
    turn_separator: Optional[str] = None
    border: Optional[str] = None
    content_width: Optional[int] = None

    def apply_to(self, theme: Any) -> None:
        for field_name in (
            "body", "muted", "dim", "heading", "accent",
            "success", "info_text", "warning_text", "error_text", "blocked_text",
            "info_bg", "ok_bg", "warn_bg", "error_bg", "status_bg",
            "blocked_bg", "spinner_bg", "muted_bg",
            "assistant_label", "user_label", "turn_separator", "border",
            "content_width",
        ):
            value = getattr(self, field_name, None)
            if value is not None:
                setattr(theme, field_name, value)


@dataclass
class CliConfig:
    path: Path
    exists: bool
    defaults: CliDefaults
    ui: CliUi
    theme: CliTheme = field(default_factory=CliTheme)
    mcp_servers: list = field(default_factory=list)
    mcp_errors: list = field(default_factory=list)


def _optional_str(value: Any, field_name: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"Config field '{field_name}' must be a string.")
    return value


def _optional_bool(value: Any, field_name: str) -> Optional[bool]:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ValueError(f"Config field '{field_name}' must be a boolean.")
    return value


def _optional_int(value: Any, field_name: str) -> Optional[int]:
    if value is None:
        return None
    if not isinstance(value, int):
        raise ValueError(f"Config field '{field_name}' must be an integer.")
    return value


def _optional_string_list(value: Any, field_name: str) -> Optional[list[str]]:
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(
            f"Config field '{field_name}' must be a list of strings."
        )
    return value


def _expand(value: str) -> str:
    """Expand $VAR / ${VAR} so secrets can live in the environment, not the file."""
    return os.path.expandvars(value)


def _optional_string_map(value: Any, field_name: str) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in value.items()
    ):
        raise ValueError(f"Config field '{field_name}' must be a table of string values.")
    return {k: _expand(v) for k, v in value.items()}


def parse_mcp_servers(raw: Any) -> tuple[list, list[str]]:
    """Parse the `[mcp_servers]` table into McpServerConfig objects.

    Returns (servers, errors). A malformed entry is reported and skipped rather
    than raising, so one bad server does not make the whole config unloadable.
    """
    from apsara_cli.engine.mcp_client import McpServerConfig

    servers: list = []
    errors: list[str] = []

    if raw is None:
        return servers, errors
    if not isinstance(raw, dict):
        errors.append("Config section 'mcp_servers' must be a table.")
        return servers, errors

    for name, entry in raw.items():
        label = f"mcp_servers.{name}"
        if not isinstance(entry, dict):
            errors.append(f"Config section '{label}' must be a table.")
            continue
        try:
            args_raw = _optional_string_list(entry.get("args"), f"{label}.args") or []
            timeout = entry.get("timeout")
            if timeout is not None and not isinstance(timeout, (int, float)):
                raise ValueError(f"Config field '{label}.timeout' must be a number.")

            command = _optional_str(entry.get("command"), f"{label}.command")
            url = _optional_str(entry.get("url"), f"{label}.url")
            cwd = _optional_str(entry.get("cwd"), f"{label}.cwd")
            enabled = _optional_bool(entry.get("enabled"), f"{label}.enabled")

            servers.append(
                McpServerConfig(
                    name=name,
                    command=_expand(command) if command else None,
                    args=[_expand(arg) for arg in args_raw],
                    env=_optional_string_map(entry.get("env"), f"{label}.env"),
                    cwd=_expand(cwd) if cwd else None,
                    url=_expand(url) if url else None,
                    headers=_optional_string_map(entry.get("headers"), f"{label}.headers"),
                    enabled=True if enabled is None else enabled,
                    timeout=float(timeout) if timeout is not None else 30.0,
                )
            )
        except ValueError as exc:
            errors.append(str(exc))

    return servers, errors


def project_config_path(base_dir: Path) -> Path:
    return base_dir / LOCAL_CONFIG_DIRNAME / LOCAL_CONFIG_FILENAME


def find_project_config(start_dir: Path) -> Optional[Path]:
    current = start_dir.resolve()
    for candidate_dir in (current, *current.parents):
        candidate = project_config_path(candidate_dir)
        if candidate.exists():
            return candidate
    return None


def resolve_cli_config_path(
    config_path: Optional[str] = None,
    workspace_hint: Optional[str] = None,
) -> Path:
    if config_path:
        return Path(config_path).expanduser().resolve()

    cwd = Path.cwd().resolve()
    if workspace_hint:
        workspace = Path(workspace_hint).expanduser()
        if not workspace.is_absolute():
            workspace = (cwd / workspace).resolve()
        else:
            workspace = workspace.resolve()
        workspace_config = find_project_config(workspace)
        if workspace_config is not None:
            return workspace_config

    cwd_config = find_project_config(cwd)
    if cwd_config is not None:
        return cwd_config

    if DEFAULT_CONFIG_PATH.exists():
        return DEFAULT_CONFIG_PATH

    if workspace_hint:
        workspace = Path(workspace_hint).expanduser()
        if not workspace.is_absolute():
            workspace = (cwd / workspace).resolve()
        else:
            workspace = workspace.resolve()
        return project_config_path(workspace)

    return project_config_path(cwd)


def load_cli_config(
    config_path: Optional[str] = None,
    workspace_hint: Optional[str] = None,
) -> CliConfig:
    path = resolve_cli_config_path(config_path, workspace_hint)
    if not path.exists():
        return CliConfig(
            path=path,
            exists=False,
            defaults=CliDefaults(),
            ui=CliUi(),
        )

    parsed = tomllib.loads(path.read_text(encoding="utf-8"))
    defaults_raw = parsed.get("defaults", {})
    if not isinstance(defaults_raw, dict):
        raise ValueError("Config section 'defaults' must be a table.")
    ui_raw = parsed.get("ui", {})
    if not isinstance(ui_raw, dict):
        raise ValueError("Config section 'ui' must be a table.")

    defaults = CliDefaults(
        workspace=_optional_str(defaults_raw.get("workspace"), "defaults.workspace"),
        model=_optional_str(defaults_raw.get("model"), "defaults.model"),
        session=_optional_str(defaults_raw.get("session"), "defaults.session"),
        stateless=_optional_bool(defaults_raw.get("stateless"), "defaults.stateless"),
        allow_bash=_optional_bool(
            defaults_raw.get("allow_bash"), "defaults.allow_bash"
        ),
        allowed_commands=_optional_string_list(
            defaults_raw.get("allowed_commands"),
            "defaults.allowed_commands",
        ),
        max_file_size=_optional_int(
            defaults_raw.get("max_file_size"), "defaults.max_file_size"
        ),
        bash_timeout=_optional_int(
            defaults_raw.get("bash_timeout"), "defaults.bash_timeout"
        ),
        auto_approve=_optional_bool(
            defaults_raw.get("auto_approve"), "defaults.auto_approve"
        ),
        color=_optional_bool(defaults_raw.get("color"), "defaults.color"),
    )
    ui = CliUi(
        welcome_title=_optional_str(ui_raw.get("welcome_title"), "ui.welcome_title"),
        welcome_subtitle=_optional_str(
            ui_raw.get("welcome_subtitle"), "ui.welcome_subtitle"
        ),
        powered_by=_optional_str(ui_raw.get("powered_by"), "ui.powered_by"),
        welcome_animation=_optional_bool(
            ui_raw.get("welcome_animation"), "ui.welcome_animation"
        ),
        welcome_frame_delay_ms=_optional_int(
            ui_raw.get("welcome_frame_delay_ms"), "ui.welcome_frame_delay_ms"
        ),
        mascot=_optional_bool(ui_raw.get("mascot"), "ui.mascot"),
        mascot_animation=_optional_bool(ui_raw.get("mascot_animation"), "ui.mascot_animation"),
    )

    theme_raw = ui_raw.get("theme", {})
    if not isinstance(theme_raw, dict):
        theme_raw = {}
    theme = CliTheme(
        body=_optional_str(theme_raw.get("body"), "ui.theme.body"),
        muted=_optional_str(theme_raw.get("muted"), "ui.theme.muted"),
        dim=_optional_str(theme_raw.get("dim"), "ui.theme.dim"),
        heading=_optional_str(theme_raw.get("heading"), "ui.theme.heading"),
        accent=_optional_str(theme_raw.get("accent"), "ui.theme.accent"),
        success=_optional_str(theme_raw.get("success"), "ui.theme.success"),
        info_text=_optional_str(theme_raw.get("info_text"), "ui.theme.info_text"),
        warning_text=_optional_str(theme_raw.get("warning_text"), "ui.theme.warning_text"),
        error_text=_optional_str(theme_raw.get("error_text"), "ui.theme.error_text"),
        blocked_text=_optional_str(theme_raw.get("blocked_text"), "ui.theme.blocked_text"),
        info_bg=_optional_str(theme_raw.get("info_bg"), "ui.theme.info_bg"),
        ok_bg=_optional_str(theme_raw.get("ok_bg"), "ui.theme.ok_bg"),
        warn_bg=_optional_str(theme_raw.get("warn_bg"), "ui.theme.warn_bg"),
        error_bg=_optional_str(theme_raw.get("error_bg"), "ui.theme.error_bg"),
        status_bg=_optional_str(theme_raw.get("status_bg"), "ui.theme.status_bg"),
        blocked_bg=_optional_str(theme_raw.get("blocked_bg"), "ui.theme.blocked_bg"),
        spinner_bg=_optional_str(theme_raw.get("spinner_bg"), "ui.theme.spinner_bg"),
        muted_bg=_optional_str(theme_raw.get("muted_bg"), "ui.theme.muted_bg"),
        assistant_label=_optional_str(theme_raw.get("assistant_label"), "ui.theme.assistant_label"),
        user_label=_optional_str(theme_raw.get("user_label"), "ui.theme.user_label"),
        turn_separator=_optional_str(theme_raw.get("turn_separator"), "ui.theme.turn_separator"),
        border=_optional_str(theme_raw.get("border"), "ui.theme.border"),
        content_width=_optional_int(theme_raw.get("content_width"), "ui.theme.content_width"),
    )

    mcp_servers, mcp_errors = parse_mcp_servers(parsed.get("mcp_servers"))

    return CliConfig(
        path=path,
        exists=True,
        defaults=defaults,
        ui=ui,
        theme=theme,
        mcp_servers=mcp_servers,
        mcp_errors=mcp_errors,
    )
