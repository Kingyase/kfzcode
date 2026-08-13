"""Agent 基类 — 消息循环 + LLM 调用 + 工具执行"""
import asyncio
import json
from abc import ABC, abstractmethod
from pathlib import Path

from .messages import AgentRole, Message, MessageType
from .message_bus import MessageBus, AgentTimeoutError
from .task_context import TaskContext, USER_RESPONSE_TIMEOUT
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
        # 对话历史按 task_id 隔离（per-task 状态）
        self._histories: dict[str, list[LLMMessage]] = {}
        # 当前正在处理的任务 id（串行调度下同一时刻只有一个）
        self._current_task_id: str = ""

    @property
    @abstractmethod
    def system_prompt(self) -> str:
        """每个 Agent 角色有独立的 system prompt"""
        ...

    @abstractmethod
    async def handle_message(self, msg: Message) -> None:
        """处理收到的消息"""
        ...

    # ===== per-task 状态访问 =====
    def _history(self, task_id: str = "") -> list[LLMMessage]:
        """返回指定任务（默认当前任务）的对话历史。"""
        tid = task_id or self._current_task_id
        return self._histories.setdefault(tid, [])

    def _task_context(self, task_id: str = "") -> TaskContext | None:
        """返回指定任务（默认当前任务）的 TaskContext。"""
        tid = task_id or self._current_task_id
        if not tid:
            return None
        return self.bus.get_task_context(tid)

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
                self._current_task_id = msg.task_id
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
        """调用 DeepV4，并把结果追加到当前任务的对话历史。"""
        history = self._history()
        history.append(LLMMessage(role="user", content=user_message))

        all_messages = [
            LLMMessage(role="system", content=self.system_prompt)
        ] + history

        response = await self.llm.chat(all_messages, tools=tools, stream=True)
        history.append(response.to_message())
        return response

    async def execute_tool_loop(self, initial_response: LLMResponse,
                                 tools: list, max_turns: int = 20) -> LLMResponse:
        """执行 tool-use 循环（含 ask_user 暂停和 require_confirm 前置检查）"""
        current_response = initial_response
        turns = 0
        ctx = self._task_context()

        while current_response.tool_calls and turns < max_turns:
            if ctx and ctx.is_cancelled():
                break  # 任务被取消，终止工具循环
            turns += 1
            tool_results: list[ToolResult] = []

            for tc in current_response.tool_calls:
                tool = self.registry.get(tc.name)

                # ==== ask_user: 发布事件并等待用户回复（多 Agent 交互闭环） ====
                if tc.name == "ask_user":
                    try:
                        args = json.loads(tc.arguments) if isinstance(tc.arguments, str) else tc.arguments
                    except (json.JSONDecodeError, TypeError):
                        args = {}
                    await self.bus.publish_event("progress", {
                        "phase": "ask_user",
                        "agent": self.role.value,
                        "question": args.get("question", ""),
                        "question_type": args.get("question_type", "text"),
                        "header": args.get("header", ""),
                        "options": args.get("options", []),
                        "multi_select": args.get("multi_select", False),
                    }, task_id=self._current_task_id)
                    # 等待用户回复（阻塞直到 HTTP respond 或超时）
                    reply = await ctx.wait_for_user_response(timeout=USER_RESPONSE_TIMEOUT) if ctx else {"status": "timeout"}
                    if ctx and ctx.is_cancelled():
                        return current_response  # 任务被取消，提前结束工具循环
                    answer = reply.get("answer", "")
                    if reply.get("status") == "timeout" or not answer:
                        answer = "(用户未回复)"
                    result = ToolResult(
                        tool_call_id=tc.id, name=tc.name, success=True,
                        output=f"用户回复: {answer}",
                    )
                    result.tool_call_id = tc.id
                    result.name = tc.name
                    tool_results.append(result)
                    self._history().append(result.to_message())
                    continue  # 继续本轮后续工具调用

                # ==== require_confirm: 等待用户确认后决定是否执行（确认闭环） ====
                if tool and tool.require_confirm:
                    await self.bus.publish_event("progress", {
                        "phase": "need_confirm",
                        "agent": self.role.value,
                        "name": tc.name,
                        "arguments": tc.arguments,
                    }, task_id=self._current_task_id)
                    reply = await ctx.wait_for_user_response(timeout=USER_RESPONSE_TIMEOUT) if ctx else {"status": "timeout"}
                    if ctx and ctx.is_cancelled():
                        return current_response  # 任务被取消，提前结束工具循环
                    approved = reply.get("approved", False)
                    custom_message = reply.get("custom_message", "")
                    if approved:
                        # 用户确认，执行工具
                        try:
                            args = json.loads(tc.arguments) if isinstance(tc.arguments, str) else tc.arguments
                            result = await tool.execute(**args)
                            result.tool_call_id = tc.id
                            result.name = tc.name
                            if custom_message:
                                result.output = f"[用户备注: {custom_message}]\n{result.output}"
                        except Exception as e:
                            result = ToolResult(
                                tool_call_id=tc.id, name=tc.name, success=False,
                                output="", error=str(e)
                            )
                    else:
                        # 用户拒绝，跳过执行
                        result = ToolResult(
                            tool_call_id=tc.id, name=tc.name, success=False,
                            output="⏭ 用户拒绝执行",
                            error="用户拒绝执行",
                        )
                    result.tool_call_id = tc.id
                    result.name = tc.name
                    tool_results.append(result)
                    self._history().append(result.to_message())
                    continue

                if not tool:
                    result = ToolResult(
                        tool_call_id=tc.id, name=tc.name, success=False,
                        output="", error=f"未知工具: {tc.name}"
                    )
                else:
                    try:
                        args = json.loads(tc.arguments) if isinstance(tc.arguments, str) else tc.arguments
                        # 回滚追踪：写文件前备份原内容、shell 命令标记
                        self._track_rollback(tc.name, tool, args)
                        result = await tool.execute(**args)
                        result.tool_call_id = tc.id
                        result.name = tc.name
                    except Exception as e:
                        result = ToolResult(
                            tool_call_id=tc.id, name=tc.name, success=False,
                            output="", error=str(e)
                        )

                tool_results.append(result)
                self._history().append(result.to_message())

            # 发送进度事件
            await self.bus.publish_event("tool_results", {
                "agent": self.role.value,
                "results": [
                    {"name": r.name, "success": r.success, "output": r.output[:200]}
                    for r in tool_results
                ],
            }, task_id=self._current_task_id)

            # 继续 LLM 调用
            history = self._history()
            all_messages = [LLMMessage(role="system", content=self.system_prompt)] + history
            current_response = await self.llm.chat(all_messages, tools=tools, stream=True)
            history.append(current_response.to_message())

        return current_response

    def reset_context(self, task_id: str = "") -> None:
        """重置对话上下文（指定任务或全部）。"""
        if task_id:
            self._histories.pop(task_id, None)
        else:
            self._histories.clear()

    def _track_rollback(self, tool_name: str, tool, args: dict) -> None:
        """工具执行前的回滚追踪（写文件备份原内容、shell 标记）。

        由 execute_tool_loop 在调用工具前调用，配合 TaskContext 的回滚字段，
        实现「取消时回滚当前任务的文件修改」。
        """
        ctx = self._task_context()
        if not ctx:
            return

        if tool_name in ("write_file", "edit_file"):
            file_path = args.get("file_path", "")
            if not file_path:
                return
            path = Path(file_path)
            if not path.is_absolute():
                workspace_root = getattr(tool, "workspace_root", None) or Path.cwd()
                path = workspace_root / path
            path = path.resolve()
            key = str(path)
            if path.exists():
                try:
                    ctx.backup_file(key, path.read_bytes())
                except Exception:
                    pass
            else:
                ctx.mark_created(key)
        elif tool_name == "execute_command":
            ctx.mark_shell_used()

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
