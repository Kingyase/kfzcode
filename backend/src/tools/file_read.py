"""文件读取工具"""
from pathlib import Path

from .base import ToolBase
from ..llm.types import ToolResult


class FileReadTool(ToolBase):
    name = "read_file"
    description = "读取指定文件的内容。支持分页读取大文件。"
    parameters = {
        "file_path": {
            "type": "string",
            "description": "要读取的文件路径（相对于项目根目录的绝对或相对路径）",
        },
        "offset": {
            "type": "integer",
            "description": "从第几行开始读取（1-indexed）",
            "default": 1,
        },
        "limit": {
            "type": "integer",
            "description": "读取行数上限",
            "default": 200,
        },
    }
    require_confirm = False  # 读取文件自动放行

    def __init__(self, workspace_root: str):
        self.workspace_root = Path(workspace_root).resolve()

    async def execute(self, file_path: str, offset: int = 1, limit: int = 200) -> ToolResult:
        try:
            path = self._resolve_path(file_path)
            if not path.exists():
                return ToolResult(
                    tool_call_id="", name=self.name, success=False,
                    output="", error=f"文件不存在: {path}"
                )
            if not path.is_file():
                return ToolResult(
                    tool_call_id="", name=self.name, success=False,
                    output="", error=f"不是文件: {path}"
                )

            content = path.read_text(encoding="utf-8", errors="replace")
            lines = content.splitlines()
            total_lines = len(lines)

            offset = max(1, offset) - 1
            selected = lines[offset:offset + limit]

            if offset + limit < total_lines:
                output = f"[文件: {path.name} | 共 {total_lines} 行 | 显示 {offset + 1}-{offset + len(selected)} 行]"
            else:
                output = f"[文件: {path.name} | 共 {total_lines} 行]"

            for i, line in enumerate(selected, start=offset + 1):
                output += f"\n{i:>6} | {line}"

            return ToolResult(
                tool_call_id="", name=self.name, success=True, output=output,
            )
        except UnicodeDecodeError:
            return ToolResult(
                tool_call_id="", name=self.name, success=False,
                output="", error="无法以文本方式读取此文件（可能是二进制文件）"
            )
        except Exception as e:
            return ToolResult(
                tool_call_id="", name=self.name, success=False,
                output="", error=str(e)
            )

    def _resolve_path(self, file_path: str) -> Path:
        path = Path(file_path)
        if not path.is_absolute():
            path = self.workspace_root / path
        return path.resolve()


class ListDirectoryTool(ToolBase):
    name = "list_directory"
    description = "列出指定目录的内容结构"
    parameters = {
        "directory": {
            "type": "string",
            "description": "要列出的目录路径（默认项目根目录）",
            "default": ".",
        },
        "depth": {
            "type": "integer",
            "description": "遍历深度",
            "default": 2,
        },
    }
    require_confirm = False

    def __init__(self, workspace_root: str):
        self.workspace_root = Path(workspace_root).resolve()

    async def execute(self, directory: str = ".", depth: int = 2) -> ToolResult:
        try:
            root = self._resolve_path(directory)
            if not root.exists():
                return ToolResult(
                    tool_call_id="", name=self.name, success=False,
                    output="", error=f"目录不存在: {root}"
                )

            output = self._tree(root, depth)
            return ToolResult(
                tool_call_id="", name=self.name, success=True,
                output=f"[{root}]\n{output}",
            )
        except Exception as e:
            return ToolResult(
                tool_call_id="", name=self.name, success=False,
                output="", error=str(e)
            )

    def _resolve_path(self, directory: str) -> Path:
        path = Path(directory)
        if not path.is_absolute():
            path = self.workspace_root / path
        return path.resolve()

    def _tree(self, root: Path, max_depth: int, current_depth: int = 0,
              prefix: str = "", ignore_patterns: set | None = None) -> str:
        if ignore_patterns is None:
            ignore_patterns = {".git", "__pycache__", "node_modules",
                               ".venv", "venv", ".idea", ".vscode", ".DS_Store"}
        if current_depth > max_depth:
            return ""

        lines = []
        try:
            entries = sorted(root.iterdir(), key=lambda e: (e.is_file(), e.name))
        except PermissionError:
            return f"{prefix}[权限不足]"

        for i, entry in enumerate(entries):
            if entry.name in ignore_patterns or entry.name.startswith("."):
                continue

            is_last = i == len(entries) - 1
            connector = "└── " if is_last else "├── "
            lines.append(f"{prefix}{connector}{entry.name}")

            if entry.is_dir():
                next_prefix = prefix + ("    " if is_last else "│   ")
                lines.append(self._tree(entry, max_depth, current_depth + 1, next_prefix))

        return "\n".join(line for line in lines if line)
