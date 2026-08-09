"""工具抽象基类与注册机制"""
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from ..llm.types import ToolDefinition, ToolResult


@dataclass
class ToolCallRequest:
    """一个待执行的工具调用"""
    tool_call_id: str
    name: str
    arguments: dict[str, Any]


class ToolBase(ABC):
    """工具抽象基类"""

    name: str = ""
    description: str = ""
    parameters: dict[str, Any] = {}
    require_confirm: bool = False  # 是否需要用户确认

    def to_definition(self) -> ToolDefinition:
        """转换为 OpenAI function calling 格式"""
        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": self.parameters,
                "required": self._get_required_params(),
            },
        )

    def _get_required_params(self) -> list[str]:
        """从 parameters 中推断必填字段（没有 default 的即为必填）"""
        return [k for k, v in self.parameters.items()
                if isinstance(v, dict) and "default" not in v]

    @abstractmethod
    async def execute(self, **kwargs) -> ToolResult:
        """执行工具，子类实现"""
        ...

    async def cancel(self) -> None:
        """可选的取消回调，子类可覆盖以处理清理逻辑（如 kill 子进程）

        当用户请求终止当前任务时，SingleAgentRunner 会调用此方法。
        默认实现为空 — 不影响文件读写等瞬时操作的工具。
        """
        pass

    def validate_args(self, **kwargs) -> dict[str, Any]:
        """校验并补全参数默认值"""
        validated = {}
        for key, schema in self.parameters.items():
            if key in kwargs:
                validated[key] = kwargs[key]
            elif isinstance(schema, dict) and "default" in schema:
                validated[key] = schema["default"]
            else:
                raise ValueError(f"缺少必填参数: {key}")
        return validated


class ToolRegistry:
    """工具注册中心"""

    def __init__(self):
        self._tools: dict[str, ToolBase] = {}

    def register(self, tool: ToolBase) -> None:
        """注册工具"""
        if not tool.name:
            raise ValueError(f"工具 {tool.__class__.__name__} 缺少 name 属性")
        self._tools[tool.name] = tool

    def register_many(self, tools: list[ToolBase]) -> None:
        """批量注册工具"""
        for tool in tools:
            self.register(tool)

    def get(self, name: str) -> ToolBase | None:
        """获取工具实例"""
        return self._tools.get(name)

    def get_definitions(self, disabled: list[str] | None = None) -> list[ToolDefinition]:
        """获取所有工具定义（排除禁用的）"""
        disabled = disabled or []
        return [t.to_definition() for name, t in self._tools.items()
                if name not in disabled]

    def parse_tool_calls(self, raw_calls: list[dict]) -> list[ToolCallRequest]:
        """将 LLM 返回的 raw tool_calls 转换为 ToolCallRequest"""
        requests = []
        for call in raw_calls:
            func = call.get("function", {})
            args_str = func.get("arguments", "{}")
            try:
                args = json.loads(args_str) if isinstance(args_str, str) else args_str
            except json.JSONDecodeError:
                args = {}
            requests.append(ToolCallRequest(
                tool_call_id=call.get("id", ""),
                name=func.get("name", ""),
                arguments=args,
            ))
        return requests

    def list_tools(self) -> list[dict]:
        """列出所有已注册工具及其信息"""
        return [
            {
                "name": t.name,
                "description": t.description,
                "require_confirm": t.require_confirm,
            }
            for t in self._tools.values()
        ]
