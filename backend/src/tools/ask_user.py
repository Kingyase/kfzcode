"""用户交互工具 — Agent 主动反问"""
from .base import ToolBase
from ..llm.types import ToolResult


class AskUserTool(ToolBase):
    name = "ask_user"
    description = (
        "【必须使用】当遇到以下任一情况时，立即调用此工具向用户提问，绝不能自行猜测或假设："
        "1) 用户需求表述模糊、有多种理解方式；"
        "2) 存在多个合理的技术方案需要选择（框架、库、架构模式等）；"
        "3) 缺少关键实现信息（配置值、路径、凭证等）；"
        "4) 操作为破坏性（删除文件、覆盖配置、修改生产代码）需二次确认；"
        "5) 任务涉及多个步骤但用户未指定优先级。"
        "不要害怕打扰用户——错误猜测比多问一句的成本高得多。"
    )
    parameters = {
        "question": {
            "type": "string",
            "description": "要问用户的问题",
        },
        "question_type": {
            "type": "string",
            "enum": ["text", "single_choice", "multi_choice", "confirm"],
            "description": "问题类型: text=文本输入, single_choice=单选, multi_choice=多选, confirm=确认/取消",
        },
        "header": {
            "type": "string",
            "description": "简短分类标签（≤12字符），如 '认证方案'",
        },
        "options": {
            "type": "array",
            "description": "选项列表（single_choice/multi_choice 时必填）",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string", "description": "选项标签"},
                    "description": {"type": "string", "description": "选项说明"},
                },
            },
            "default": [],
        },
        "multi_select": {
            "type": "boolean",
            "description": "是否允许多选（仅 multi_choice 时有效）",
            "default": False,
        },
    }
    require_confirm = False  # ask_user 本身不需要确认

    async def execute(self, question: str, question_type: str = "text",
                      header: str = "", options: list | None = None,
                      multi_select: bool = False) -> ToolResult:
        options = options or []
        # ask_user 是特殊工具：它会暂停 Agent 循环，前端捕获后展示交互式 UI
        # 用户回复后会重新注入到 Agent 上下文，工具本身不返回最终结果
        return ToolResult(
            tool_call_id="", name=self.name, success=True,
            output=f"[等待用户回复] {question}",
        )
