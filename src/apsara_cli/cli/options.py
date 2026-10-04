import argparse
import os
from pathlib import Path
from typing import Any, Optional, Set

from dotenv import dotenv_values

from apsara_cli.shared.types import ResolvedOptions
from apsara_cli.shared.ui import default_use_color


def resolve_workspace(path_str: str) -> Path:
    return Path(path_str).expanduser().resolve()


def resolve_value(explicit: Any, config_value: Any, fallback: Any) -> Any:
    if explicit is not None:
        return explicit
    if config_value is not None:
        return config_value
    return fallback


# Named bundles for --allowed-commands, referenced as @name.
#
# The default allowlist is read-only tools that duplicate what the agent can
# already do natively, so out of the box it cannot run the thing that proves its
# work. @verify fixes that; it is opt-in because running a project's build
# scripts is a real trust decision.
COMMAND_PRESETS: dict[str, Set[str]] = {
    "verify": {
        # Python
        "pytest", "python", "python3", "tox", "ruff", "mypy",
        # JS/TS
        "npm", "npx", "pnpm", "yarn", "node", "tsc", "jest", "vitest", "eslint",
        # Other toolchains
        "go", "cargo", "make", "mvn", "gradle", "dotnet",
    },
    "read": {"pwd", "ls", "find", "rg", "cat", "sed", "head", "tail", "wc"},
    "git": {"git"},
}

# A repository-controlled .env is convenient for BYO provider credentials, but
# it is not a trusted place for Apsara runtime policy. In particular, projects
# must not be able to raise token budgets, redirect API endpoints, replace the
# pricing cache, or enable fallbacks merely because the user opened the folder.
# Explicit shell environment variables and user config remain available for
# intentional advanced configuration.
WORKSPACE_DOTENV_CREDENTIAL_KEYS = frozenset({
    "ANTHROPIC_API_KEY",
    "AZURE_API_KEY",
    "AZURE_OPENAI_API_KEY",
    "CEREBRAS_API_KEY",
    "COHERE_API_KEY",
    "DEEPSEEK_API_KEY",
    "FIREWORKS_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "GROQ_API_KEY",
    "MISTRAL_API_KEY",
    "NVIDIA_API_KEY",
    "OPENAI_API_KEY",
    "OPENCODE_API_KEY",
    "OPENROUTER_API_KEY",
    "TOGETHER_API_KEY",
    "XAI_API_KEY",
})


def expand_command_presets(commands: Set[str]) -> Set[str]:
    """Replace @preset entries with their commands, leaving others untouched."""
    expanded: Set[str] = set()
    for entry in commands:
        if entry.startswith("@"):
            name = entry[1:]
            if name not in COMMAND_PRESETS:
                known = ", ".join("@" + key for key in sorted(COMMAND_PRESETS))
                raise ValueError(
                    f"Unknown command preset '{entry}'. Available presets: {known}."
                )
            expanded |= COMMAND_PRESETS[name]
        else:
            expanded.add(entry)
    return expanded


def parse_allowed_commands(raw_commands: Any) -> Optional[Set[str]]:
    if raw_commands is None:
        return None
    if isinstance(raw_commands, str):
        commands = {part.strip() for part in raw_commands.split(",") if part.strip()}
    elif isinstance(raw_commands, list):
        commands = {str(item).strip() for item in raw_commands if str(item).strip()}
    else:
        raise ValueError("Allowed commands must be a comma-separated string or a list.")

    if not commands:
        raise ValueError("Allowed commands cannot be empty when provided.")
    return expand_command_presets(commands)


def resolve_runtime_options(args: argparse.Namespace, config_defaults: Any) -> ResolvedOptions:
    from apsara_cli.engine.models import DEFAULT_MODEL, resolve_model_id as _resolve_mid
    from apsara_cli.cli.session import latest_session_name, new_session_name

    workspace = resolve_value(args.workspace, config_defaults.workspace, ".")
    workspace_root = resolve_workspace(str(workspace))
    # Precedence: --model flag > config default > Apsara's OpenCode Zen default.
    # Stored credentials provide keys, not a hidden startup-model override.
    _raw_model = resolve_value(args.model, config_defaults.model, None)
    if _raw_model is None:
        _raw_model = DEFAULT_MODEL
    model = _resolve_mid(str(_raw_model))  # expand short aliases at startup

    # Starting Apsara should mean starting a new conversation. Existing history
    # is loaded only when the user explicitly names a session, asks to continue
    # the latest one, or has deliberately pinned a session in config.
    explicit_session = getattr(args, "session", None)
    continue_session = bool(getattr(args, "continue_session", False))
    configured_session = getattr(config_defaults, "session", None)
    if explicit_session is not None:
        session = str(explicit_session)
    elif continue_session:
        session = latest_session_name(workspace_root) or new_session_name()
    elif configured_session is not None:
        session = str(configured_session)
    else:
        session = new_session_name()

    stateless = bool(resolve_value(args.stateless, config_defaults.stateless, False))
    allow_bash = bool(resolve_value(args.allow_bash, config_defaults.allow_bash, False))
    allowed_commands = parse_allowed_commands(
        resolve_value(args.allowed_commands, config_defaults.allowed_commands, None)
    )
    max_file_size = resolve_value(args.max_file_size, config_defaults.max_file_size, None)
    bash_timeout = resolve_value(
        getattr(args, "bash_timeout", None),
        getattr(config_defaults, "bash_timeout", None),
        None,
    )
    auto_approve = bool(resolve_value(args.auto_approve, config_defaults.auto_approve, False))
    use_color = bool(resolve_value(args.color, config_defaults.color, default_use_color()))
    dry_run = bool(getattr(args, "dry_run", False))
    read_only = bool(getattr(args, "read_only", False))

    return ResolvedOptions(
        workspace_root=workspace_root,
        model=str(model),
        session=str(session),
        stateless=stateless,
        allow_bash=allow_bash,
        allowed_commands=allowed_commands,
        max_file_size=max_file_size,
        auto_approve=auto_approve,
        use_color=use_color,
        dry_run=dry_run,
        read_only=read_only,
        bash_timeout=int(bash_timeout) if bash_timeout is not None else None,
    )


def load_cli_environment(args: argparse.Namespace, config: Any) -> list[Path]:
    workspace = resolve_value(getattr(args, "workspace", None), config.defaults.workspace, ".")
    candidates = [resolve_workspace(str(workspace)), Path.cwd().resolve()]

    loaded_paths: list[Path] = []
    seen_paths: set[Path] = set()
    for base_path in candidates:
        env_path = base_path / ".env"
        if env_path in seen_paths or not env_path.exists():
            continue
        seen_paths.add(env_path)

        values = dotenv_values(env_path)
        loaded_any = False
        for key, value in values.items():
            if (
                value is None
                or key not in WORKSPACE_DOTENV_CREDENTIAL_KEYS
                or key in os.environ
            ):
                continue
            os.environ[key] = value
            loaded_any = True

        if loaded_any:
            loaded_paths.append(env_path)

    return loaded_paths


def detect_model_credentials(model: str) -> tuple[str, Optional[list[str]], str]:
    raw_model = model.strip()
    provider = None
    model_name = raw_model

    if "/" in raw_model:
        provider, model_name = raw_model.split("/", 1)
        provider = provider.lower()
    normalized_name = model_name.lower()

    if provider == "opencode" or normalized_name == "big-pickle":
        return (
            "opencode",
            ["OPENCODE_API_KEY"],
            "OpenCode Zen model detected.",
        )

    if provider in {"openai", "azure", "azure_openai"} or normalized_name.startswith(
        ("gpt-", "o1", "o3", "o4", "o5", "codex-", "text-embedding-")
    ):
        if provider in {"azure", "azure_openai"}:
            return ("azure-openai", ["AZURE_OPENAI_API_KEY", "AZURE_API_KEY"], "Azure OpenAI-style model detected.")
        return ("openai", ["OPENAI_API_KEY"], "OpenAI-style model detected.")

    if provider == "anthropic" or normalized_name.startswith("claude"):
        return ("anthropic", ["ANTHROPIC_API_KEY"], "Anthropic-style model detected.")

    if provider in {"gemini", "google"} or normalized_name.startswith("gemini"):
        return ("gemini", ["GEMINI_API_KEY", "GOOGLE_API_KEY"], "Gemini-style model detected.")

    if provider == "groq":
        return ("groq", ["GROQ_API_KEY"], "Groq-style model detected.")

    if provider in {"together", "together_ai"}:
        return ("together", ["TOGETHER_API_KEY"], "Together-style model detected.")

    if provider == "mistral" or normalized_name.startswith("mistral"):
        return ("mistral", ["MISTRAL_API_KEY"], "Mistral-style model detected.")

    if provider == "xai":
        return ("xai", ["XAI_API_KEY"], "xAI-style model detected.")

    if provider == "deepseek" or normalized_name.startswith("deepseek"):
        return ("deepseek", ["DEEPSEEK_API_KEY"], "DeepSeek-style model detected.")

    if provider == "openrouter":
        return ("openrouter", ["OPENROUTER_API_KEY"], "OpenRouter-style model detected.")

    if provider in {"fireworks", "fireworks_ai"}:
        return ("fireworks", ["FIREWORKS_API_KEY"], "Fireworks-style model detected.")

    if provider == "cohere" or normalized_name.startswith("command"):
        return ("cohere", ["COHERE_API_KEY"], "Cohere-style model detected.")

    if provider == "cerebras":
        return ("cerebras", ["CEREBRAS_API_KEY"], "Cerebras-style model detected.")

    if provider == "bedrock":
        return ("bedrock", ["AWS_ACCESS_KEY_ID", "AWS_PROFILE"], "Bedrock-style model detected.")

    if provider == "vertex_ai":
        return ("vertex_ai", ["GOOGLE_APPLICATION_CREDENTIALS"], "Vertex AI-style model detected.")

    if provider == "nvidia":
        return ("nvidia", ["NVIDIA_API_KEY"], "NVIDIA-style model detected.")

    if provider == "ollama":
        return ("ollama", None, "Ollama-style local model detected; no API key required.")

    return ("unknown", None, f"Could not infer credentials for model '{model}'.")
