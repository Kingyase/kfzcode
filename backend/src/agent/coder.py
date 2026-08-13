"""Coder Agent — 专注编码实现"""
from dataclasses import asdict

from .base_agent import BaseAgent
from .messages import (
    AgentRole, Message, MessageType,
    CoderTaskPayload, CoderResultPayload, FixInstructionPayload,
)
from .message_bus import MessageBus
from ..llm.client import DeepV4Client
from ..llm.types import LLMResponse, FunctionCall
from ..tools.base import ToolRegistry


class CoderAgent(BaseAgent):
    """编码执行 Agent"""

    def __init__(self, bus: MessageBus, llm: DeepV4Client, registry: ToolRegistry, workspace: str):
        super().__init__(AgentRole.CODER, bus, llm, registry)
        self.workspace = workspace

    @property
    def system_prompt(self) -> str:
        return f"""你是 KFZCode 的代码执行 Agent (Coder)。你的职责是编写高质量的代码。

## 工作原则
1. **先理解再动手**: 在修改代码前，先阅读相关文件了解项目结构和代码风格
2. **遵循现有风格**: 匹配项目的代码风格、命名约定和架构模式
3. **最小化变更**: 只修改必要的部分，不要重构无关代码
4. **编写测试**: 如果任务要求测试，务必编写并以 `pytest` 或项目使用的测试框架运行
5. **不确定时反问**: 遇到模糊需求时，使用 ask_user 工具向用户确认，不要猜测
6. **验证你的代码**: 写完代码后运行 lint 或测试确保正确

## 可用工具
- read_file: 读取项目文件内容
- write_file: 创建或覆盖文件
- edit_file: 精确字符串替换编辑
- execute_command: 运行 shell 命令（如测试、构建）
- grep: 正则搜索文件内容
- glob: 文件名模式匹配搜索
- list_directory: 查看目录结构
- ask_user: 向用户提问

## 当前工作目录
{self.workspace}
"""

    async def handle_message(self, msg: Message) -> None:
        if msg.type == MessageType.TASK_ASSIGN:
            await self._handle_coding_task(msg)
        elif msg.type == MessageType.FIX_INSTRUCTION:
            await self._handle_fix_task(msg)
        elif msg.type == MessageType.CANCEL:
            self.reset_context(msg.task_id)

    async def _handle_coding_task(self, msg: Message) -> None:
        """处理编码任务"""
        payload = CoderTaskPayload.from_dict(msg.payload)
        self.reset_context(msg.task_id)

        await self.bus.publish_event("progress", {
            "agent": "coder",
            "status": "working",
            "task": payload.description,
            "iteration": msg.iteration,
        }, task_id=msg.task_id)

        # 构建编码任务 prompt
        prompt = self._build_coding_prompt(payload)
        tools = self.registry.get_definitions()

        # 执行 tool-use 循环
        response = await self.call_llm(prompt, tools=tools)

        if response.tool_calls:
            response = await self.execute_tool_loop(response, tools)

        # 收集变更
        result = CoderResultPayload(
            status="success" if response.finish_reason == "stop" else "partial",
            changed_files=self._collect_changes(),
            created_files=self._collect_created(),
            notes=response.content,
            warnings=[],
        )

        await self.send_result(msg, result.to_dict())

    async def _handle_fix_task(self, msg: Message) -> None:
        """处理修复任务"""
        payload = FixInstructionPayload.from_dict(msg.payload)

        await self.bus.publish_event("progress", {
            "agent": "coder",
            "status": "fixing",
            "issue_count": len(payload.issues_to_fix),
            "iteration": msg.iteration,
        }, task_id=msg.task_id)

        prompt = self._build_fix_prompt(payload)
        tools = self.registry.get_definitions()

        response = await self.call_llm(prompt, tools=tools)
        if response.tool_calls:
            response = await self.execute_tool_loop(response, tools)

        result = CoderResultPayload(
            status="success" if response.finish_reason == "stop" else "partial",
            changed_files=self._collect_changes(),
            notes=response.content,
        )

        await self.send_result(msg, result.to_dict())

    def _build_coding_prompt(self, p: CoderTaskPayload) -> str:
        parts = [f"## 编码任务\n\n{p.description}\n"]

        if p.constraints:
            parts.append("## 约束条件\n")
            for c in p.constraints:
                parts.append(f"- {c}")
            parts.append("")

        if p.context_snapshot:
            parts.append("## 相关文件内容\n")
            for path, content in p.context_snapshot.items():
                if len(content) > 3000:
                    content = content[:3000] + "\n... [内容截断]"
                parts.append(f"### {path}\n```\n{content}\n```\n")

        if p.project_conventions:
            parts.append(f"## 项目约定\n{p.project_conventions}\n")

        parts.append("\n请开始实现。先分析需求，然后逐步执行。")
        return "\n".join(parts)

    def _build_fix_prompt(self, p: FixInstructionPayload) -> str:
        parts = ["## 修复任务\n\n以下问题需要修复:\n"]

        for i, issue in enumerate(p.issues_to_fix, 1):
            parts.append(f"### 问题 {i}: [{issue.severity.upper()}] {issue.title}")
            parts.append(f"- 文件: {issue.file_path}" + (f":{issue.line_number}" if issue.line_number else ""))
            parts.append(f"- 问题描述: {issue.description}")
            parts.append(f"- 期望行为: {issue.expected_behavior}")
            parts.append(f"- 实际行为: {issue.actual_behavior}")
            parts.append(f"- 修复建议: {issue.fix_suggestion}")
            parts.append("")

        if p.additional_context:
            parts.append(f"## 补充说明\n{p.additional_context}\n")

        parts.append("\n请逐个修复以上问题。")
        return "\n".join(parts)

    def _collect_changes(self) -> list[dict]:
        """从对话历史中收集文件变更"""
        changes = []
        for msg in self._history():
            if msg.role == "tool" and msg.content:
                content = msg.content
                if "✓" in content and ("创建" in content or "覆盖" in content or "编辑" in content):
                    changes.append({
                        "tool": msg.name,
                        "summary": content[:200],
                    })
        return changes

    def _collect_created(self) -> list[str]:
        """收集新创建的文件"""
        created = []
        for msg in self._history():
            if msg.role == "tool" and msg.content and "✓ 创建文件:" in msg.content:
                created.append(msg.content.split("✓ 创建文件:")[-1].split("(")[0].strip())
        return created
