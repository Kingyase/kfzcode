"""文件写入工具"""
from pathlib import Path

from .base import ToolBase
from ..llm.types import ToolResult


class FileWriteTool(ToolBase):
    name = "write_file"
    description = "创建新文件或覆盖已有文件的内容"
    parameters = {
        "file_path": {
            "type": "string",
            "description": "要写入的文件路径",
        },
        "content": {
            "type": "string",
            "description": "要写入的完整文件内容",
        },
    }
    require_confirm = True  # 写文件需要确认

    def __init__(self, workspace_root: str):
        self.workspace_root = Path(workspace_root).resolve()

    async def execute(self, file_path: str, content: str) -> ToolResult:
        try:
            path = self._resolve_path(file_path)
            parent = path.parent
            parent.mkdir(parents=True, exist_ok=True)

            is_new = not path.exists()
            path.write_text(content, encoding="utf-8")

            line_count = content.count("\n") + 1
            action = "创建" if is_new else "覆盖写入"
            return ToolResult(
                tool_call_id="", name=self.name, success=True,
                output=f"✓ {action}文件: {path} ({line_count}行, {len(content)}字符)",
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


class FileEditTool(ToolBase):
    name = "edit_file"
    description = "精确字符串替换编辑。在文件中查找 old_string 并替换为 new_string。old_string 必须在文件中唯一匹配。"
    parameters = {
        "file_path": {
            "type": "string",
            "description": "要编辑的文件路径",
        },
        "old_string": {
            "type": "string",
            "description": "要被替换的原始文本（必须与文件中内容完全一致）",
        },
        "new_string": {
            "type": "string",
            "description": "替换后的新文本",
        },
        "replace_all": {
            "type": "boolean",
            "description": "是否替换所有匹配项",
            "default": False,
        },
    }
    require_confirm = True  # 编辑文件需要确认

    def __init__(self, workspace_root: str):
        self.workspace_root = Path(workspace_root).resolve()

    async def execute(self, file_path: str, old_string: str,
                      new_string: str, replace_all: bool = False) -> ToolResult:
        try:
            path = self._resolve_path(file_path)
            if not path.exists():
                return ToolResult(
                    tool_call_id="", name=self.name, success=False,
                    output="", error=f"文件不存在: {path}"
                )

            content = path.read_text(encoding="utf-8")

            if replace_all:
                count = content.count(old_string)
                if count == 0:
                    return ToolResult(
                        tool_call_id="", name=self.name, success=False,
                        output="", error="old_string 在文件中未找到"
                    )
                new_content = content.replace(old_string, new_string)
            else:
                count = content.count(old_string)
                if count == 0:
                    return ToolResult(
                        tool_call_id="", name=self.name, success=False,
                        output="", error="old_string 在文件中未找到"
                    )
                if count > 1:
                    return ToolResult(
                        tool_call_id="", name=self.name, success=False,
                        output="", error=f"old_string 匹配到 {count} 处，请使用 replace_all=true 或提供更精确的匹配文本"
                    )
                new_content = content.replace(old_string, new_string, 1)

            path.write_text(new_content, encoding="utf-8")

            # 生成简短 diff 摘要
            old_lines = old_string.count("\n") + 1
            new_lines = new_string.count("\n") + 1
            diff_desc = f"-{old_lines}行 +{new_lines}行" if old_lines != new_lines else f"修改{old_lines}行"

            return ToolResult(
                tool_call_id="", name=self.name, success=True,
                output=f"✓ 编辑文件: {path} ({diff_desc}, 共{count}处匹配)",
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
