"""Agent 间通信协议 — 所有消息类型定义"""
from dataclasses import dataclass, field
from enum import Enum
import uuid
import time
from typing import Any


class AgentRole(Enum):
    ORCHESTRATOR = "orchestrator"
    CODER = "coder"
    TESTER = "tester"


class MessageType(Enum):
    TASK_ASSIGN = "task_assign"
    TASK_RESULT = "task_result"
    FIX_INSTRUCTION = "fix_instruction"
    PROGRESS_UPDATE = "progress_update"
    CANCEL = "cancel"
    HEARTBEAT = "heartbeat"


@dataclass
class Message:
    """消息总线中的一条消息"""
    type: MessageType
    from_agent: AgentRole
    to_agent: AgentRole
    task_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    iteration: int = 0
    payload: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    timestamp: float = field(default_factory=time.time)


# ===== Payload 数据结构 =====

@dataclass
class CoderTaskPayload:
    """Orchestrator → Coder: 编码任务"""
    description: str
    context_snapshot: dict[str, str] = field(default_factory=dict)
    constraints: list[str] = field(default_factory=list)
    files_to_modify: list[str] | None = None
    project_conventions: str = ""

    def to_dict(self) -> dict:
        return {
            "description": self.description,
            "context_snapshot": self.context_snapshot,
            "constraints": self.constraints,
            "files_to_modify": self.files_to_modify,
            "project_conventions": self.project_conventions,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CoderTaskPayload":
        return cls(
            description=data.get("description", ""),
            context_snapshot=data.get("context_snapshot", {}),
            constraints=data.get("constraints", []),
            files_to_modify=data.get("files_to_modify"),
            project_conventions=data.get("project_conventions", ""),
        )


@dataclass
class CoderResultPayload:
    """Coder → Orchestrator: 编码结果"""
    status: str  # "success" | "partial" | "failed"
    changed_files: list[dict] = field(default_factory=list)
    created_files: list[str] = field(default_factory=list)
    test_code_included: bool = False
    notes: str = ""
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "changed_files": self.changed_files,
            "created_files": self.created_files,
            "test_code_included": self.test_code_included,
            "notes": self.notes,
            "warnings": self.warnings,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CoderResultPayload":
        return cls(
            status=data.get("status", "failed"),
            changed_files=data.get("changed_files", []),
            created_files=data.get("created_files", []),
            test_code_included=data.get("test_code_included", False),
            notes=data.get("notes", ""),
            warnings=data.get("warnings", []),
        )


@dataclass
class TestTaskPayload:
    """Orchestrator → Tester: 验证任务"""
    coder_result: CoderResultPayload = field(default_factory=CoderResultPayload)
    test_commands: list[str] = field(default_factory=list)
    review_dimensions: list[str] = field(default_factory=lambda: ["correctness", "security", "style"])
    changed_files_content: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "coder_result": self.coder_result.to_dict(),
            "test_commands": self.test_commands,
            "review_dimensions": self.review_dimensions,
            "changed_files_content": self.changed_files_content,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TestTaskPayload":
        return cls(
            coder_result=CoderResultPayload.from_dict(data.get("coder_result", {})),
            test_commands=data.get("test_commands", []),
            review_dimensions=data.get("review_dimensions", ["correctness", "security", "style"]),
            changed_files_content=data.get("changed_files_content", {}),
        )


@dataclass
class Issue:
    """Tester 发现的问题"""
    severity: str      # "critical" | "warning" | "suggestion"
    category: str      # "test_failure" | "lint" | "type_error" | "security" | "style" | "logic"
    file_path: str
    line_number: int | None = None
    title: str = ""
    description: str = ""
    expected_behavior: str = ""
    actual_behavior: str = ""
    fix_suggestion: str = ""

    def to_dict(self) -> dict:
        return {
            "severity": self.severity,
            "category": self.category,
            "file_path": self.file_path,
            "line_number": self.line_number,
            "title": self.title,
            "description": self.description,
            "expected_behavior": self.expected_behavior,
            "actual_behavior": self.actual_behavior,
            "fix_suggestion": self.fix_suggestion,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Issue":
        return cls(
            severity=data.get("severity", "warning"),
            category=data.get("category", "style"),
            file_path=data.get("file_path", ""),
            line_number=data.get("line_number"),
            title=data.get("title", ""),
            description=data.get("description", ""),
            expected_behavior=data.get("expected_behavior", ""),
            actual_behavior=data.get("actual_behavior", ""),
            fix_suggestion=data.get("fix_suggestion", ""),
        )


@dataclass
class TestResultPayload:
    """Tester → Orchestrator: 验证结果"""
    passed: bool
    test_output: str = ""
    issues: list[Issue] = field(default_factory=list)
    review_summary: str = ""

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "test_output": self.test_output,
            "issues": [i.to_dict() for i in self.issues],
            "review_summary": self.review_summary,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TestResultPayload":
        return cls(
            passed=data.get("passed", False),
            test_output=data.get("test_output", ""),
            issues=[Issue.from_dict(i) for i in data.get("issues", [])],
            review_summary=data.get("review_summary", ""),
        )


@dataclass
class FixInstructionPayload:
    """Orchestrator → Coder: 修复指令"""
    original_task: CoderTaskPayload = field(default_factory=CoderTaskPayload)
    issues_to_fix: list[Issue] = field(default_factory=list)
    priority_order: list[str] = field(default_factory=lambda: ["critical_first"])
    additional_context: str = ""

    def to_dict(self) -> dict:
        return {
            "original_task": self.original_task.to_dict(),
            "issues_to_fix": [i.to_dict() for i in self.issues_to_fix],
            "priority_order": self.priority_order,
            "additional_context": self.additional_context,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "FixInstructionPayload":
        return cls(
            original_task=CoderTaskPayload.from_dict(data.get("original_task", {})),
            issues_to_fix=[Issue.from_dict(i) for i in data.get("issues_to_fix", [])],
            priority_order=data.get("priority_order", ["critical_first"]),
            additional_context=data.get("additional_context", ""),
        )
