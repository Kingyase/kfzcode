"""代码搜索工具 — grep + glob"""
import re
import fnmatch
from pathlib import Path

from .base import ToolBase
from ..llm.types import ToolResult


class GrepTool(ToolBase):
    name = "grep"
    description = "在项目中用正则表达式搜索文件内容。返回匹配的文件路径、行号和内容。"
    parameters = {
        "pattern": {
            "type": "string",
            "description": "正则表达式搜索模式",
        },
        "directory": {
            "type": "string",
            "description": "搜索目录（默认项目根目录）",
            "default": ".",
        },
        "include": {
            "type": "string",
            "description": "文件名过滤（glob 模式），如 '*.py' 或 '*.{ts,js}'",
            "default": "",
        },
        "max_results": {
            "type": "integer",
            "description": "最大返回结果数",
            "default": 50,
        },
        "context_lines": {
            "type": "integer",
            "description": "上下文行数",
            "default": 0,
        },
    }
    require_confirm = False

    IGNORE_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv",
                   ".idea", ".vscode", "dist", "build", "target"}

    def __init__(self, workspace_root: str):
        self.workspace_root = Path(workspace_root).resolve()

    async def execute(self, pattern: str, directory: str = ".",
                      include: str = "", max_results: int = 50,
                      context_lines: int = 0) -> ToolResult:
        try:
            root = self._resolve_dir(directory)
            if not root.exists():
                return ToolResult(
                    tool_call_id="", name=self.name, success=False,
                    output="", error=f"目录不存在: {root}"
                )

            results = []
            compiled = re.compile(pattern, re.IGNORECASE)

            for file_path in self._walk_files(root, include):
                try:
                    content = file_path.read_text(encoding="utf-8", errors="replace")
                    lines = content.splitlines()
                    for i, line in enumerate(lines):
                        if compiled.search(line):
                            rel_path = file_path.relative_to(root)
                            entry = f"{rel_path}:{i + 1}: {line.strip()[:200]}"

                            if context_lines > 0:
                                for ctx_line in lines[max(0, i - context_lines):i]:
                                    entry += f"\n  ...  {ctx_line.strip()[:150]}"
                                entry += f"\n  >>> {line.strip()[:200]}"
                                for ctx_line in lines[i + 1:min(len(lines), i + 1 + context_lines)]:
                                    entry += f"\n  ...  {ctx_line.strip()[:150]}"

                            results.append(entry)
                            if len(results) >= max_results:
                                break
                    if len(results) >= max_results:
                        break
                except (UnicodeDecodeError, PermissionError):
                    continue

            if not results:
                return ToolResult(
                    tool_call_id="", name=self.name, success=True,
                    output=f"未找到匹配 '{pattern}' 的结果"
                )

            output = f"搜索 '{pattern}' 找到 {len(results)} 个结果:\n" + "\n".join(results)
            if len(results) >= max_results:
                output += f"\n\n[结果已截断，只显示前 {max_results} 条]"

            return ToolResult(tool_call_id="", name=self.name, success=True, output=output)
        except re.error as e:
            return ToolResult(
                tool_call_id="", name=self.name, success=False,
                output="", error=f"正则表达式错误: {e}"
            )
        except Exception as e:
            return ToolResult(
                tool_call_id="", name=self.name, success=False, output="", error=str(e)
            )

    def _walk_files(self, root: Path, include: str):
        for path in root.rglob("*"):
            if any(p in self.IGNORE_DIRS for p in path.parts):
                continue
            if not path.is_file():
                continue
            if include and not fnmatch.fnmatch(path.name, include):
                continue
            yield path

    def _resolve_dir(self, directory: str) -> Path:
        path = Path(directory)
        if not path.is_absolute():
            path = self.workspace_root / path
        return path.resolve()


class GlobTool(ToolBase):
    name = "glob"
    description = "按文件名模式匹配查找文件。支持通配符如 '**/*.py'、'*.ts'、'src/**/*.test.*'"
    parameters = {
        "pattern": {
            "type": "string",
            "description": "Glob 匹配模式，如 '**/*.py'",
        },
        "directory": {
            "type": "string",
            "description": "搜索目录（默认项目根目录）",
            "default": ".",
        },
    }
    require_confirm = False

    IGNORE_DIRS = GrepTool.IGNORE_DIRS

    def __init__(self, workspace_root: str):
        self.workspace_root = Path(workspace_root).resolve()

    async def execute(self, pattern: str, directory: str = ".") -> ToolResult:
        try:
            root = self._resolve_dir(directory)
            if not root.exists():
                return ToolResult(
                    tool_call_id="", name=self.name, success=False,
                    output="", error=f"目录不存在: {root}"
                )

            matches = []
            for path in root.rglob(pattern):
                if any(p in self.IGNORE_DIRS for p in path.parts):
                    continue
                if path.is_file():
                    size = path.stat().st_size
                    size_str = self._format_size(size)
                    rel = path.relative_to(root)
                    matches.append(f"  {rel} ({size_str})")

            if not matches:
                return ToolResult(
                    tool_call_id="", name=self.name, success=True,
                    output=f"未找到匹配 '{pattern}' 的文件"
                )

            output = f"匹配 '{pattern}' 找到 {len(matches)} 个文件:\n" + "\n".join(matches[:100])
            if len(matches) > 100:
                output += f"\n\n[结果已截断，只显示前 100 个]"

            return ToolResult(tool_call_id="", name=self.name, success=True, output=output)
        except Exception as e:
            return ToolResult(
                tool_call_id="", name=self.name, success=False, output="", error=str(e)
            )

    def _resolve_dir(self, directory: str) -> Path:
        path = Path(directory)
        if not path.is_absolute():
            path = self.workspace_root / path
        return path.resolve()

    @staticmethod
    def _format_size(size: int) -> str:
        if size < 1024:
            return f"{size}B"
        elif size < 1024 * 1024:
            return f"{size / 1024:.1f}KB"
        else:
            return f"{size / (1024 * 1024):.1f}MB"
