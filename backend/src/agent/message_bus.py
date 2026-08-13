"""Agent 间消息总线 — 基于 asyncio.Queue"""
import asyncio
from collections import defaultdict

from .messages import Message, AgentRole
from .task_context import TaskContext


class AgentTimeoutError(asyncio.TimeoutError):
    """Agent 等待消息超时"""
    pass


class MessageBus:
    """Agent 间消息总线"""

    def __init__(self):
        self._queues: dict[AgentRole, asyncio.Queue[Message]] = {
            AgentRole.ORCHESTRATOR: asyncio.Queue(maxsize=200),
            AgentRole.CODER: asyncio.Queue(maxsize=200),
            AgentRole.TESTER: asyncio.Queue(maxsize=200),
        }
        # 事件总线：每个监听项为 (task_id, queue)。
        # task_id 为空串表示"订阅所有任务"（向后兼容/全局事件）。
        self._event_listeners: dict[str, list[tuple[str, asyncio.Queue]]] = defaultdict(list)
        # 请求-响应追踪
        self._pending_requests: dict[str, asyncio.Future] = {}
        self._pending_targets: dict[str, AgentRole] = {}
        # 任务上下文（per-task 状态隔离）
        self._tasks: dict[str, TaskContext] = {}

    # ===== 任务上下文管理 =====
    def register_task(self, task_id: str, session_id: str = "") -> TaskContext:
        """注册一个新任务，返回其独立的 TaskContext。"""
        ctx = TaskContext(task_id=task_id, session_id=session_id)
        self._tasks[task_id] = ctx
        return ctx

    def get_task_context(self, task_id: str) -> TaskContext | None:
        """按 task_id 获取任务上下文；不存在时返回 None。"""
        return self._tasks.get(task_id)

    def remove_task(self, task_id: str) -> None:
        """移除任务上下文（任务结束后清理），并兜底释放备份内存。"""
        ctx = self._tasks.pop(task_id, None)
        if ctx:
            ctx.clear_backups()

    # ===== 消息传递 =====
    async def send(self, msg: Message) -> None:
        """发送消息到目标 Agent"""
        await self._queues[msg.to_agent].put(msg)

    async def receive(self, agent: AgentRole, timeout: float | None = None) -> Message:
        """Agent 阻塞等待消息"""
        try:
            return await asyncio.wait_for(
                self._queues[agent].get(), timeout=timeout
            )
        except asyncio.TimeoutError:
            raise AgentTimeoutError(f"Agent {agent.value} 等待消息超时")

    async def request(self, msg: Message, timeout: float = 300.0) -> Message:
        """同步请求-响应模式"""
        loop = asyncio.get_event_loop()
        future: asyncio.Future[Message] = loop.create_future()
        self._pending_requests[msg.id] = future
        self._pending_targets[msg.id] = msg.to_agent
        await self.send(msg)
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending_requests.pop(msg.id, None)
            self._pending_targets.pop(msg.id, None)
            raise AgentTimeoutError(f"请求 {msg.id} 超时 ({timeout}s)")

    async def reply(self, original_msg: Message, reply_msg: Message) -> None:
        """回复一个请求"""
        if original_msg.id in self._pending_requests:
            future = self._pending_requests.pop(original_msg.id)
            self._pending_targets.pop(original_msg.id, None)
            if not future.done():
                future.set_result(reply_msg)
        else:
            await self.send(reply_msg)

    def cancel_pending(self, agent: AgentRole, reason: str = "Agent 处理异常") -> None:
        """取消指定 AgentRole 的所有待处理请求（Agent 崩溃时清理 Future）"""
        cancelled = []
        for msg_id, future in list(self._pending_requests.items()):
            target = self._pending_targets.get(msg_id)
            if target == agent and not future.done():
                try:
                    future.set_exception(AgentTimeoutError(
                        f"Agent {agent.value} 异常，请求 {msg_id} 已取消: {reason}"
                    ))
                except Exception:
                    pass  # Future 可能已被其他地方取消
                cancelled.append(msg_id)
        for msg_id in cancelled:
            self._pending_requests.pop(msg_id, None)
            self._pending_targets.pop(msg_id, None)

    async def broadcast(self, msg: Message) -> None:
        """广播给所有 Agent"""
        for agent in AgentRole:
            await self._queues[agent].put(Message(
                type=msg.type,
                from_agent=msg.from_agent,
                to_agent=agent,
                task_id=msg.task_id,
                iteration=msg.iteration,
                payload=dict(msg.payload),
            ))

    # ===== 事件发布/订阅（带 task_id 过滤） =====
    async def publish_event(self, event_type: str, data: dict, task_id: str = "") -> None:
        """发布事件。

        task_id 为空串表示广播给所有订阅者（全局事件）；
        非空时只发给订阅了相同 task_id 或订阅了"所有"（task_id=""）的队列。
        """
        for tid, queue in self._event_listeners.get(event_type, []):
            if not task_id or not tid or tid == task_id:
                await queue.put(data)

    def subscribe(self, event_type: str, task_id: str = "") -> asyncio.Queue:
        """订阅某类事件。

        task_id 为空串表示订阅所有任务的事件；非空时只收该任务的事件。
        """
        q: asyncio.Queue = asyncio.Queue()
        self._event_listeners[event_type].append((task_id, q))
        return q

    def unsubscribe(self, event_type: str, queue: asyncio.Queue) -> None:
        """取消订阅"""
        for item in self._event_listeners.get(event_type, []):
            if item[1] is queue:
                self._event_listeners[event_type].remove(item)
                break
