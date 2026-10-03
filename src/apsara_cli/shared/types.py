from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from time import time
from typing import Any, Optional, Set
from uuid import uuid4


class AgentRunState(str, Enum):
    CREATED = "created"
    PLANNING = "planning"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    COMPLETED_VERIFIED = "completed_verified"
    COMPLETED_UNVERIFIED = "completed_unverified"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ToolRisk(str, Enum):
    READ = "read"
    WRITE = "write"
    EXECUTE = "execute"
    EXTERNAL = "external"
    DESTRUCTIVE = "destructive"


@dataclass
class ToolResult:
    ok: bool
    content: str = ""
    error: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_text(self) -> str:
        if self.ok:
            return self.content
        message = self.error or self.content or "Tool failed"
        return message if message.lstrip().lower().startswith("error") else f"Error: {message}"

    @classmethod
    def from_text(cls, value: Any) -> "ToolResult":
        text = str(value or "")
        failed = text.lstrip().lower().startswith("error")
        return cls(ok=not failed, content="" if failed else text, error=text if failed else None)


@dataclass
class AgentStep:
    kind: str
    title: str
    status: str = "pending"
    detail: str = ""
    started_at: Optional[float] = None
    finished_at: Optional[float] = None


@dataclass
class AgentRun:
    objective: str
    model: str
    workspace: str
    run_id: str = field(default_factory=lambda: uuid4().hex[:12])
    state: AgentRunState = AgentRunState.CREATED
    steps: list[AgentStep] = field(default_factory=list)
    started_at: float = field(default_factory=time)
    finished_at: Optional[float] = None
    changed_files: list[str] = field(default_factory=list)
    verification: list[str] = field(default_factory=list)
    error: Optional[str] = None
    verification_status: str = "not_required"
    critic_status: str = "not_required"
    completion_reason: Optional[str] = None
    verification_evidence: list[dict[str, Any]] = field(default_factory=list)
    critic_findings: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["state"] = self.state.value
        return data


@dataclass
class ResolvedOptions:
    workspace_root: Path
    model: str
    session: str
    stateless: bool
    allow_bash: bool
    allowed_commands: Optional[Set[str]]
    max_file_size: Optional[int]
    auto_approve: bool
    use_color: bool
    dry_run: bool = False
    read_only: bool = False
    bash_timeout: Optional[int] = None


@dataclass
class DoctorCheckResult:
    name: str
    status: str
    detail: str


@dataclass
class HiddenCliEvent:
    kind: str
    title: str
    detail: str


@dataclass
class ContextTrimResult:
    request_history: list[dict[str, Any]]
    dropped_turns: int
    dropped_messages: int
    original_tokens: int
    trimmed_tokens: int
    summary: Optional[str] = None
    auxiliary_usage: Optional[dict[str, Any]] = None
