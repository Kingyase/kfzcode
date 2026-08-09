"""Agent 基类 — 消息循环 + LLM 调用 + 工具执行"""
import asyncio
import json
from abc import ABC, abstractmethod

from .messages import AgentRole, Message, MessageType
from .message_bus import MessageBus, AgentTimeoutError
from ..llm.client import DeepV4Client
from ..llm.types import LLMMessage, LLMResponse, ToolResult
from ..tools.base import ToolRegistry


class BaseAgent(ABC):
    """所有 Agent 的基类"""

    def __init__(self, role: AgentRole, bus: MessageBus,
                 llm: DeepV4Client, registry: ToolRegistry):
        self.role = role
        self.bus = bus
        self.llm = llm
        self.registry = registry
        self._task: asyncio.Task | None = None
        self._running = False
        self.conversation_history: list[LLMMessage] = []

    @property
    @abstractmethod
    def system_prompt(self) -> str:
        """每个 Agent 角色有独立的 system prompt"""
        ...

    @abstractmethod
    async def handle_message(self, msg: Message) -> None:
        """处理收到的消息"""
        ...

    async def start(self) -> None:
        """启动 Agent 的消息循环"""
        self._running = True
        self._task = asyncio.create_task(self._message_loop(), name=f"agent-{self.role.value}")

    async def stop(self) -> None:
        """停止 Agent"""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _message_loop(self) -> None:
        """消息循环：持续监听消息队列"""
        while self._running:
            try:
                msg = await self.bus.receive(self.role, timeout=1.0)
                await self.handle_message(msg)
            except AgentTimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                # 记录错误但不让 Agent 崩溃
                # 取消所有等待此 Agent 回复的 pending futures，防止发送方永久阻塞
                self.bus.cancel_pending(self.role, str(e))
                await self.bus.publish_event("error", {
                    "agent": self.role.value,
                    "error": str(e),
                })

    async def call_llm(self, user_message: str, tools: list | None = None) -> LLMResponse:
        """调用 DeepV4"""
        self.conversation_history.append(LLMMessage(role="user", content=user_message))

        all_messages = [
            LLMMessage(role="system", content=self.system_prompt)
        ] + self.conversation_history

        response = await self.llm.chat(all_messages, tools=tools, stream=True)
        self.conversation_history.append(response.to_message())
        return response

    async def execute_tool_loop(self, initial_response: LLMResponse,
                                 tools: list, max_turns: int = 20) -> LLMResponse:
        """执行 tool-use 循环（含 ask_user 暂停和 require_confirm 前置检查）"""
        current_response = initial_response
        turns = 0

        while current_response.tool_calls and turns < max_turns:
            turns += 1
            tool_results: list[ToolResult] = []
            should_break = False

            for tc in current_response.tool_calls:
                tool = self.registry.get(tc.name)

                # ==== ask_user: 多 Agent 模式下发布事件、中止循环 ====
                if tc.name == "ask_user":
                    try:
                        args = json.loads(tc.arguments) if isinstance(tc.arguments, str) else tc.arguments
                    except (json.JSONDecodeError, TypeError):
                        args = {}
                    await self.bus.publish_event("ask_user", {
                        "agent": self.role.value,
                        "question": args.get("question", ""),
                        "question_type": args.get("question_type", "text"),
                        "header": args.get("header", ""),
                        "options": args.get("options", []),
                        "multi_select": args.get("multi_select", False),
                    })
                    result = ToolResult(
                        tool_call_id=tc.id, name=tc.name, success=True,
                        output=f"[等待用户回复] {args.get('question', '')}",
                    )
                    result.tool_call_id = tc.id
                    result.name = tc.name
                    tool_results.append(result)
                    self.conversation_history.append(result.to_message())
                    should_break = True
                    break  # 不再执行本轮后续工具调用

                # ==== require_confirm: 在执行前检查（不是执行后） ====
                if tool and tool.require_confirm:
                    await self.bus.publish_event("need_confirm", {
                        "agent": self.role.value,
                        "tool": tc.name,
                        "arguments": tc.arguments,
                    })
                    # 多 Agent 模式下无法真正暂停等待，跳过执行并记录
                    result = ToolResult(
                        tool_call_id=tc.id, name=tc.name, success=False,
                        output="⏭ 需要用户确认（多 Agent 模式下自动跳过）",
                        error="需要用户确认",
                    )
                    result.tool_call_id = tc.id
                    result.name = tc.name
                    tool_results.append(result)
                    self.conversation_history.append(result.to_message())
                    continue

                if not tool:
                    result = ToolResult(
                        tool_call_id=tc.id, name=tc.name, success=False,
                        output="", error=f"未知工具: {tc.name}"
                    )
                else:
                    try:
                        args = json.loads(tc.arguments) if isinstance(tc.arguments, str) else tc.arguments
                        result = await tool.execute(**args)
                        result.tool_call_id = tc.id
                        result.name = tc.name
                    except Exception as e:
                        result = ToolResult(
                            tool_call_id=tc.id, name=tc.name, success=False,
                            output="", error=str(e)
                        )

                tool_results.append(result)
                self.conversation_history.append(result.to_message())

            # 发送进度事件
            await self.bus.publish_event("tool_results", {
                "agent": self.role.value,
                "results": [
                    {"name": r.name, "success": r.success, "output": r.output[:200]}
                    for r in tool_results
                ],
            })

            if should_break:
                break

            # 继续 LLM 调用
            all_messages = [LLMMessage(role="system", content=self.system_prompt)] + self.conversation_history
            current_response = await self.llm.chat(all_messages, tools=tools, stream=True)
            self.conversation_history.append(current_response.to_message())

        return current_response

    def reset_context(self) -> None:
        """重置对话上下文"""
        self.conversation_history = []

    async def send_result(self, original_msg: Message, result_data: dict) -> None:
        """发送任务结果"""
        await self.bus.reply(original_msg, Message(
            type=MessageType.TASK_RESULT,
            from_agent=self.role,
            to_agent=original_msg.from_agent,
            task_id=original_msg.task_id,
            iteration=original_msg.iteration,
            payload=result_data,
        ))
