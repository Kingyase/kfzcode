"""Agent 引擎 — 单 Agent 模式入口 + 多 Agent 系统初始化"""
import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

import httpx
import logging

from .message_bus import MessageBus
from .messages import AgentRole, Message, MessageType
from .task_context import TaskContext
from .coder import CoderAgent
from .reviewer import ReviewerAgent
from .orchestrator import OrchestratorAgent
from ..config import KFZCodeConfig
from ..llm.client import DeepV4Client
from ..llm.types import LLMMessage, LLMResponse, FunctionCall, ToolCallDelta, ToolResult
from ..tools.base import ToolRegistry


logger = logging.getLogger(__name__)


class KFZCodeEngine:
    """KFZCode 核心引擎 — 管理所有 Agent 的生命周期"""

    def __init__(self, config: KFZCodeConfig):
        self.config = config
        self.workspace = str(Path.cwd())

        # LLM 客户端
        self.llm = DeepV4Client(
            base_url=config.model.base_url,
            api_key=config.model.api_key,
            model=config.model.name,
            max_tokens=config.model.max_tokens,
            temperature=config.model.temperature,
            top_p=config.model.top_p,
        )

        # 工具注册
        self.tool_registry = ToolRegistry()
        self._register_tools()

        # 消息总线
        self.bus = MessageBus()

        # Agent 实例（延迟创建）
        self.coder: CoderAgent | None = None
        self.reviewer: ReviewerAgent | None = None
        self.orchestrator: OrchestratorAgent | None = None

        self._running = False

    def _register_tools(self) -> None:
        """注册所有工具"""
        from ..tools.file_read import FileReadTool, ListDirectoryTool
        from ..tools.file_write import FileWriteTool, FileEditTool
        from ..tools.shell import ShellTool
        from ..tools.search import GrepTool, GlobTool
        from ..tools.ask_user import AskUserTool

        ws = self.workspace
        self.tool_registry.register_many([
            FileReadTool(ws),
            ListDirectoryTool(ws),
            FileWriteTool(ws),
            FileEditTool(ws),
            ShellTool(
                ws,
                allowed_commands=self.config.tools.allow_commands,
                deny_commands=self.config.tools.deny_commands,
            ),
            GrepTool(ws),
            GlobTool(ws),
            AskUserTool(),
        ])

    async def start(self) -> None:
        """启动所有 Agent"""
        self._running = True

        # 创建 Agent 实例
        self.coder = CoderAgent(self.bus, self.llm, self.tool_registry, self.workspace)
        self.reviewer = ReviewerAgent(self.bus, self.llm, self.tool_registry, self.workspace)
        self.orchestrator = OrchestratorAgent(
            self.bus, self.llm, self.tool_registry, self.workspace,
            max_iterations=self.config.orchestrator.max_iterations,
            auto_confirm_on_pass=self.config.orchestrator.auto_confirm_on_pass,
            review_timeout=self.config.orchestrator.review_timeout,
            rollback_callback=self.rollback_task,
        )

        # 启动消息循环
        await self.coder.start()
        await self.reviewer.start()
        await self.orchestrator.start()

    async def stop(self) -> None:
        """停止所有 Agent"""
        self._running = False
        for agent in [self.coder, self.reviewer, self.orchestrator]:
            if agent:
                await agent.stop()
        await self.llm.close()

    async def submit_task(self, description: str, task_id: str = "", session_id: str = "") -> str:
        """提交用户任务，返回 task_id"""
        # task_id 为空时生成唯一 id（Message 的 default_factory 只在缺省时生效，显式传空串会覆盖）
        if not task_id:
            task_id = uuid.uuid4().hex[:12]
        msg = Message(
            type=MessageType.TASK_ASSIGN,
            from_agent=AgentRole.ORCHESTRATOR,
            to_agent=AgentRole.ORCHESTRATOR,
            task_id=task_id,
            payload={"description": description, "session_id": session_id},
        )
        # 注册任务上下文（per-task 状态隔离：取消标志、HITL 等待各任务独立）
        ctx = self.bus.register_task(task_id, session_id)
        # 记录 git 基线（供取消时回滚 shell 改动兜底）
        await self._capture_git_baseline(ctx)
        await self.bus.send(msg)
        return task_id

    def get_event_queue(self, task_id: str = "") -> asyncio.Queue:
        """获取事件队列（按 task_id 过滤，供 SSE 桥接使用）"""
        return self.bus.subscribe("progress", task_id)

    def respond_user(self, task_id: str, data: dict) -> None:
        """接收用户回复（多 Agent 模式的 ask_user 闭环）

        由 HTTP 端点 POST /api/chat/respond 调用，唤醒指定任务中等待用户回复的 Agent。
        """
        ctx = self.bus.get_task_context(task_id)
        if ctx:
            ctx.respond_user(data)

    def cancel_task(self, task_id: str) -> None:
        """取消指定多 Agent 任务（中途取消）

        由 HTTP 端点 POST /api/chat/cancel 调用。
        设置该任务的取消标志并唤醒其正在等待用户回复的 Agent。
        """
        ctx = self.bus.get_task_context(task_id)
        if ctx:
            ctx.cancel()

    async def _capture_git_baseline(self, ctx) -> None:
        """记录任务开始时的 git 基线（HEAD + 已有 dirty 文件），用于回滚兜底。

        非 git 仓库或 git 不可用时静默降级为「纯文件备份回滚」。
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "rev-parse", "HEAD",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                cwd=self.workspace,
            )
            stdout, _ = await proc.communicate()
            if proc.returncode == 0:
                ctx.git_head = stdout.decode(errors="replace").strip()

            proc2 = await asyncio.create_subprocess_exec(
                "git", "status", "--porcelain",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                cwd=self.workspace,
            )
            stdout2, _ = await proc2.communicate()
            if proc2.returncode == 0:
                dirty: set[str] = set()
                for line in stdout2.decode(errors="replace").splitlines():
                    if line.strip():
                        dirty.add(line[3:].strip())  # porcelain: "XY path"
                ctx.git_dirty_before = dirty
        except Exception:
            pass

    async def rollback_task(self, task_id: str) -> None:
        """回滚指定任务已做的文件修改（取消/异常时调用）。

        顺序：先精确恢复文件工具备份，再删除新建文件，最后 git 兜底 shell 改动。
        """
        ctx = self.bus.get_task_context(task_id)
        if not ctx:
            return

        # 1. 恢复被文件工具修改过的文件
        for path, content in ctx.file_backups.items():
            try:
                Path(path).write_bytes(content)
            except Exception:
                pass

        # 2. 删除本任务新建的文件
        for path in ctx.created_files:
            try:
                p = Path(path)
                if p.exists() and p.is_file():
                    p.unlink()
            except Exception:
                pass

        # 3. git 兜底 shell 造成的改动
        if ctx.shell_used and ctx.git_head:
            await self._git_rollback(ctx)

    async def _git_rollback(self, ctx) -> None:
        """用 git 兜底回滚 shell 命令造成的文件改动（排除任务前用户已有的 dirty）。"""
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "status", "--porcelain",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                cwd=self.workspace,
            )
            stdout, _ = await proc.communicate()
            if proc.returncode != 0:
                return

            dirty_now: set[str] = set()
            untracked: set[str] = set()
            for line in stdout.decode(errors="replace").splitlines():
                if not line.strip():
                    continue
                status = line[:2].strip()
                path = line[3:].strip()
                if status == "??":
                    untracked.add(path)
                else:
                    dirty_now.add(path)

            # 文件工具已精确处理过的文件（相对 workspace 路径），git 兜底跳过它们
            workspace_path = Path(self.workspace).resolve()
            handled: set[str] = set()
            for p in list(ctx.file_backups.keys()) + list(ctx.created_files):
                try:
                    rel = Path(p).resolve().relative_to(workspace_path)
                    handled.add(str(rel).replace("\\", "/"))
                except Exception:
                    pass

            # 任务期间新改动 = 当前 dirty - 任务前 dirty - 文件工具已处理
            changed_by_task = dirty_now - ctx.git_dirty_before - handled
            new_untracked = untracked - ctx.git_dirty_before - handled

            # 恢复修改/删除的已跟踪文件
            for f in changed_by_task:
                proc_r = await asyncio.create_subprocess_exec(
                    "git", "checkout", "--", f,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    cwd=self.workspace,
                )
                await proc_r.communicate()

            # 删除任务期间新增的 untracked 文件
            for f in new_untracked:
                try:
                    p = Path(self.workspace) / f
                    if p.exists() and p.is_file():
                        p.unlink()
                except Exception:
                    pass
        except Exception:
            pass

    async def health_check(self) -> dict:
        """健康检查"""
        llm_health = await self.llm.health_check()
        return {
            "status": "ok" if self._running else "stopped",
            "llm": llm_health,
            "tools": self.tool_registry.list_tools(),
            "workspace": self.workspace,
        }


class SingleAgentRunner:
    """简化单 Agent 模式 — 用于快速调试和简单任务"""

    def __init__(self, config: KFZCodeConfig, workspace: str = "",
                 session_id: str = "", auto_approve: bool = False):
        self.config = config  # 保留完整配置引用
        self.workspace = workspace or str(Path.cwd())
        self.session_id = session_id
        self.auto_approve = auto_approve
        self.auto_confirm_tools: list[str] = config.tools.auto_confirm_tools
        self.max_turns = config.behavior.max_turns  # 单次对话最大 tool-call 轮次
        self.ask_user_timeout = config.behavior.ask_user_auto_timeout  # ask_user 超时秒数
        self.llm = DeepV4Client(
            base_url=config.model.base_url,
            api_key=config.model.api_key,
            model=config.model.name,
            max_tokens=config.model.max_tokens,
            temperature=config.model.temperature,
            top_p=config.model.top_p,
        )
        self.registry = ToolRegistry()
        self._register_tools()
        self.history: list[LLMMessage] = []

        # 取消机制: 用户可通过 API 设置此事件来终止当前运行的任务
        self._cancel_event: asyncio.Event = asyncio.Event()

        # Human-in-the-Loop 暂停机制: Agent 等待用户回复时阻塞在此 Event 上
        self._user_response_event: asyncio.Event | None = None
        self._user_response_data: dict = {}
        self._ask_user_timeout_count = 0  # ask_user 连续超时计数

        # 回滚追踪：单 Agent 也支持取消时回滚本任务的文件修改
        self.task_ctx = TaskContext(task_id=session_id or "single", session_id=session_id)

        # 集成上下文管理器
        from ..agent.context import ContextManager, repair_message_history
        self.context_manager = ContextManager(max_tokens=config.model.max_tokens or 32000)
        self._repair_history = repair_message_history  # 工具函数引用

        # 集成持久化存储
        from ..agent.history import ConversationStore
        self.conversation_store = ConversationStore()

        # 尝试加载历史记录
        if session_id:
            self._load_history()

    def _register_tools(self) -> None:
        from ..tools.file_read import FileReadTool, ListDirectoryTool
        from ..tools.file_write import FileWriteTool, FileEditTool
        from ..tools.shell import ShellTool
        from ..tools.search import GrepTool, GlobTool
        from ..tools.ask_user import AskUserTool

        ws = self.workspace
        self.registry.register_many([
            FileReadTool(ws), ListDirectoryTool(ws),
            FileWriteTool(ws), FileEditTool(ws),
            ShellTool(ws), GrepTool(ws), GlobTool(ws),
            AskUserTool(),
        ])

    def _load_history(self) -> None:
        """从持久化存储加载历史记录

        加载后自动清理末尾悬空的 assistant(tool_calls) 消息，
        防止因崩溃/中断导致的不完整状态触发 LLM API 400 错误。
        """
        if not self.session_id:
            return

        try:
            data = self.conversation_store.load(self.session_id)
            if data and "messages" in data:
                self.history = [
                    LLMMessage(
                        role=msg.get("role", ""),
                        content=msg.get("content") or "",
                        reasoning_content=msg.get("reasoning_content"),
                        tool_calls=msg.get("tool_calls"),
                        tool_call_id=msg.get("tool_call_id"),
                        name=msg.get("name"),
                    )
                    for msg in data["messages"]
                ]
                # 修复损坏的 tool_calls ↔ tool_result 配对
                self.history = self._repair_history(self.history)
        except Exception:
            self.history = []

    def _repair_tool_pairings(self) -> None:
        """全量扫描并修复 history 中的 tool_calls ↔ tool_result 配对

        当对话在 tool 执行期间崩溃、中断，或 compress() 破坏了配对时，
        history 中可能出现 assistant(tool_calls) 缺少对应 tool 结果。
        直接发送给 API 会触发 400:

          "An assistant message with 'tool_calls' must be followed by tool messages"

        repair_message_history 会移除所有无法补全的悬空 assistant(tool_calls)
        及其孤立 tool 结果。
        """
        self.history = self._repair_history(self.history)

    def _save_history(self) -> None:
        """保存历史记录到持久化存储

        保存前自动清理末尾悬空的 assistant(tool_calls) 消息。
        """
        if not self.session_id:
            return

        # 保存前先修复配对（防止崩溃后残留不完整状态）
        self._repair_tool_pairings()

        try:
            title = (self.history[0].content or "")[:50] if self.history else "新对话"
            self.conversation_store.save(
                conv_id=self.session_id,
                title=title,
                messages=self.history,
                metadata={"workspace": self.workspace}
            )
        except Exception:
            pass

    def _should_auto_confirm(self, tool_name: str) -> bool:
        """判断工具是否需要跳过人工确认

        Returns True 如果满足以下任一条件:
        1. auto_approve 全局开关已开启 (CLI --auto-approve)
        2. auto_confirm_tools 配置包含 "*" 通配符
        3. 工具名称在 auto_confirm_tools 配置列表中
        """
        if self.auto_approve:
            return True
        if "*" in self.auto_confirm_tools:
            return True
        if tool_name in self.auto_confirm_tools:
            return True
        return False

    # ===== Human-in-the-Loop 暂停/恢复机制 =====

    async def wait_for_user_response(self, timeout: float = 7200.0) -> dict:
        """阻塞等待用户回复 (ask_user / need_confirm)

        创建一个 asyncio.Event 并阻塞当前协程，直到 respond_user() 被调用。
        前端通过 POST /api/chat/respond 触发 respond_user()。

        Returns:
            用户回复数据字典，超时时返回 {"status": "timeout"}
        """
        # 清理上一次可能残留的陈旧数据（防止超时后的迟来响应污染下一次等待）
        self._user_response_data = {}
        self._user_response_event = asyncio.Event()
        try:
            await asyncio.wait_for(self._user_response_event.wait(), timeout=timeout)
            return self._user_response_data
        except asyncio.TimeoutError:
            return {"status": "timeout"}
        finally:
            self._user_response_event = None

    def respond_user(self, data: dict) -> None:
        """接收用户回复，唤醒暂停的 Agent 循环

        由 POST /api/chat/respond 端点调用。
        """
        # 只有在 Agent 确实在等待时才存储数据并唤醒；
        # 如果 _user_response_event 为 None，说明 Agent 未在等待或已超时，
        # 此时不应存储数据（避免污染下一次 wait_for_user_response）
        if self._user_response_event is None:
            return
        self._user_response_data = data
        self._user_response_event.set()

    def cancel(self) -> None:
        """设置取消标志，通知正在运行的 Agent 循环终止

        会同时唤醒 wait_for_user_response()（如果正在等待用户回复）。
        由 POST /api/chat/cancel 端点调用。
        """
        self._cancel_event.set()
        # 如果正在等待用户回复（ask_user / need_confirm），立即唤醒
        if self._user_response_event is not None:
            self._user_response_event.set()

    # ===== 回滚追踪（单 Agent 取消时回滚本任务的文件修改） =====

    def _track_rollback(self, tool_name: str, tool, args: dict) -> None:
        """工具执行前的回滚追踪（写文件备份原内容、shell 标记）。"""
        if tool_name in ("write_file", "edit_file"):
            file_path = args.get("file_path", "")
            if not file_path:
                return
            path = Path(file_path)
            if not path.is_absolute():
                ws_root = getattr(tool, "workspace_root", None) or Path(self.workspace)
                path = ws_root / path
            path = path.resolve()
            key = str(path)
            if path.exists():
                try:
                    self.task_ctx.backup_file(key, path.read_bytes())
                except Exception:
                    pass
            else:
                self.task_ctx.mark_created(key)
        elif tool_name == "execute_command":
            self.task_ctx.mark_shell_used()

    async def _capture_git_baseline(self) -> None:
        """记录本次对话开始时的 git 基线（HEAD + 已有 dirty），用于回滚兜底。"""
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "rev-parse", "HEAD",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                cwd=self.workspace,
            )
            stdout, _ = await proc.communicate()
            if proc.returncode == 0:
                self.task_ctx.git_head = stdout.decode(errors="replace").strip()

            proc2 = await asyncio.create_subprocess_exec(
                "git", "status", "--porcelain",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                cwd=self.workspace,
            )
            stdout2, _ = await proc2.communicate()
            if proc2.returncode == 0:
                dirty: set[str] = set()
                for line in stdout2.decode(errors="replace").splitlines():
                    if line.strip():
                        dirty.add(line[3:].strip())
                self.task_ctx.git_dirty_before = dirty
        except Exception:
            pass

    async def _rollback(self) -> None:
        """回滚本次对话已做的文件修改（取消时调用）。"""
        # 1. 恢复文件工具备份
        for path, content in self.task_ctx.file_backups.items():
            try:
                Path(path).write_bytes(content)
            except Exception:
                pass
        # 2. 删除新建文件
        for path in self.task_ctx.created_files:
            try:
                p = Path(path)
                if p.exists() and p.is_file():
                    p.unlink()
            except Exception:
                pass
        # 3. git 兜底 shell 改动
        if self.task_ctx.shell_used and self.task_ctx.git_head:
            await self._git_rollback()

    async def _git_rollback(self) -> None:
        """git 兜底回滚 shell 改动（排除对话前已有的 dirty）。"""
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "status", "--porcelain",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                cwd=self.workspace,
            )
            stdout, _ = await proc.communicate()
            if proc.returncode != 0:
                return

            dirty_now: set[str] = set()
            untracked: set[str] = set()
            for line in stdout.decode(errors="replace").splitlines():
                if not line.strip():
                    continue
                status = line[:2].strip()
                path = line[3:].strip()
                if status == "??":
                    untracked.add(path)
                else:
                    dirty_now.add(path)

            workspace_path = Path(self.workspace).resolve()
            handled: set[str] = set()
            for p in list(self.task_ctx.file_backups.keys()) + list(self.task_ctx.created_files):
                try:
                    rel = Path(p).resolve().relative_to(workspace_path)
                    handled.add(str(rel).replace("\\", "/"))
                except Exception:
                    pass

            changed_by_task = dirty_now - self.task_ctx.git_dirty_before - handled
            new_untracked = untracked - self.task_ctx.git_dirty_before - handled

            for f in changed_by_task:
                proc_r = await asyncio.create_subprocess_exec(
                    "git", "checkout", "--", f,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    cwd=self.workspace,
                )
                await proc_r.communicate()

            for f in new_untracked:
                try:
                    p = Path(self.workspace) / f
                    if p.exists() and p.is_file():
                        p.unlink()
                except Exception:
                    pass
        except Exception:
            pass

    # ===== 主循环 =====

    async def run(self, user_message: str, image_paths: list[str] | None = None):
        """运行单次对话，实时 yield 事件: thinking, message, tool_call, tool_result, done

        Args:
            user_message: 用户文本消息
            image_paths: 可选图片文件路径列表，支持视觉识别

        包含自适应重试机制：
        - ReadError/连接中断 → 降级 max_tokens 后重试
        - 429 限流 → 指数退避等待
        - 502/503/504 → 退避重试
        - 400 请求错误 → 截断历史后重试
        """
        system_prompt = f"""你是 KFZCode AI 编程助手。
工作目录: {self.workspace}
帮助用户编写、调试、重构代码。

## 核心行为准则

### 1. 不确定时必须反问，禁止猜测
遇到以下任一情况，**必须**使用 ask_user 工具向用户提问，**绝不能**自己猜测或假设：
- 用户需求模糊、有多种理解方式（如"加个认证"→ JWT? Session? OAuth?）
- 存在多种合理的技术方案需要用户选择（如"用哪个框架？""用 SQLite 还是 MySQL？"）
- 缺少关键信息导致无法继续（如数据库地址、API密钥、端口号等）
- 用户指令可能有风险，需要二次确认（如删除文件、修改生产配置）
- 用户给了多个任务但未指定优先级或顺序

### 2. 先理解再动手
- 在修改代码前，先用 read_file / glob / grep 等工具了解项目结构和现有代码风格
- 读取关键文件后，总结理解并在 <thinking> 中推演方案，**遇到选择点时向用户确认**

### 3. 最小化变更
- 只修改必要的部分，不要顺手重构无关代码
- 遵循项目现有的命名约定、目录结构、代码风格

### 4. 验证你的工作
- 写完代码后尝试运行测试或 lint，确认没有引入错误

### 5. ⚠️ 任务完成标准 — 非常重要
- **不要提前停止**：如果你还没有完成用户要求的所有步骤，**必须继续调用工具**，不能仅输出文字说明就结束
- **纯文本回答仅用于以下两种情况**：
  1. 用户的问题是纯知识问答（如"什么是闭包？"），不需要执行任何操作
  2. 所有操作步骤已全部完成，并已验证结果正确
- **不确定是否完成时**：使用 ask_user 向用户确认"任务到此是否完成？还有什么需要调整的吗？"
- 如果已执行了工具操作（读/写/命令），在最后必须用纯文本向用户总结完成的工作和结果

## 工具选择指南
- **read_file**: 读取文件内容
- **write_file / edit_file**: 写入/编辑文件
- **execute_command**: 执行 shell 命令（测试、构建、lint）
- **grep / glob**: 搜索代码
- **list_directory**: 浏览目录结构
- **ask_user**: ⚠️ 遇到不确定性时必须调用 — 不要猜测用户意图

## 视觉能力
""" + (
    "你可以识别用户上传的图片内容（截图、架构图、UI 设计稿等），根据图片内容回答相关问题和给出建议。"
    if self.config.model.supports_vision else
    "当前模型不支持视觉识别。如果用户发送图片，请告知他们当前模型不支持多模态功能，建议切换至支持视觉的模型。"
) + """

## <thinking> 标签
用 <thinking> 标签包裹你的分析推理过程，标签外给出最终回答。
示例：
<thinking>
分析用户需求...
检查项目结构...
识别需要确认的决策点...
</thinking>

最终回答或行动..."""

        # 构建用户消息（支持纯文本或多模态图片附件）
        if image_paths and len(image_paths) > 0:
            # 早期检查：模型是否支持视觉识别
            if not self.config.model.supports_vision:
                yield {
                    "type": "error",
                    "error": (
                        f"当前模型 ({self.config.model.name}) 不支持多模态 / 视觉识别，无法处理图片。\n"
                        f"如需使用图片功能，请切换至支持视觉的模型，或在配置中设置 supports_vision: true。"
                    )
                }
                return

            from ..llm.types import image_to_content_block, build_multimodal_content

            max_size = self.config.model.max_image_size_mb * 1024 * 1024
            image_blocks: list[dict[str, Any]] = []
            for img_path in image_paths:
                p = Path(img_path)
                if not p.is_absolute():
                    p = Path(self.workspace) / p
                if not p.exists():
                    yield {"type": "error", "error": f"图片文件不存在: {img_path}"}
                    return
                if p.stat().st_size > max_size:
                    yield {
                        "type": "error",
                        "error": f"图片过大: {img_path} ({p.stat().st_size / 1024 / 1024:.1f}MB, 上限 {self.config.model.max_image_size_mb}MB)"
                    }
                    return
                try:
                    image_blocks.append(image_to_content_block(str(p)))
                except ValueError as e:
                    yield {"type": "error", "error": f"图片处理失败: {e}"}
                    return
            content = build_multimodal_content(user_message, image_blocks)
        else:
            content = user_message

        self.history.append(LLMMessage(role="user", content=content))

        # 每次新消息开始时清除上次的取消标志和超时计数
        self._cancel_event.clear()
        self._ask_user_timeout_count = 0
        # 重置回滚追踪，记录 git 基线（供取消时回滚）
        self.task_ctx.clear_backups()
        await self._capture_git_baseline()

        # ---- <thinking> 标签流式检测状态 ----
        TAG_OPEN = "<thinking>"
        TAG_CLOSE = "</thinking>"
        content_buf = ""
        in_thinking = False
        thinking_buf = ""
        has_native_reasoning = False

        def process_chunk(text: str):
            """检测 <thinking> 标签，实时分流为 thinking / message 事件"""
            nonlocal content_buf, in_thinking, thinking_buf
            combined = (thinking_buf if in_thinking else content_buf) + text

            while True:
                if in_thinking:
                    if TAG_CLOSE in combined:
                        idx = combined.index(TAG_CLOSE)
                        if combined[:idx]:
                            yield {"type": "thinking", "content": combined[:idx]}
                        combined = combined[idx + len(TAG_CLOSE):]
                        in_thinking = False
                    else:
                        keep = min(len(TAG_CLOSE) - 1, len(combined))
                        if len(combined) > keep:
                            yield {"type": "thinking", "content": combined[:-keep]}
                        thinking_buf = combined[-keep:] if keep else ""
                        return
                else:
                    if TAG_OPEN in combined:
                        idx = combined.index(TAG_OPEN)
                        if combined[:idx]:
                            yield {"type": "message", "content": combined[:idx]}
                        combined = combined[idx + len(TAG_OPEN):]
                        in_thinking = True
                    else:
                        keep = min(len(TAG_OPEN) - 1, len(combined))
                        if len(combined) > keep:
                            yield {"type": "message", "content": combined[:-keep]}
                        content_buf = combined[-keep:] if keep else ""
                        return

        turns = 0
        final_reasoning = ""
        final_status = "completed"
        current_max_tokens = self.llm.max_tokens
        ADAPTIVE_MAX_RETRIES = 2
        response = LLMResponse()  # 声明避免 UnboundLocalError

        while turns < self.max_turns:
            # ---- 取消检查点: 每轮开始前检测是否已被取消 ----
            if self._cancel_event.is_set():
                yield {"type": "message", "content": "\n⏹ 任务已被用户取消。\n"}
                final_status = "cancelled"
                # 回滚本任务已做的文件修改
                await self._rollback()
                break

            # ---- 防御: 每次 LLM 调用前修复 tool_calls 配对 ----
            self._repair_tool_pairings()

            # ---- 调用 LLM（带自适应重试） ----
            # 重置 <thinking> 标签解析状态，防止跨轮次泄漏
            content_buf = ""
            in_thinking = False
            thinking_buf = ""
            turn_reasoning = ""  # 当前轮次的推理内容（每次重试会重置）
            response = LLMResponse()
            tool_deltas: dict[int, ToolCallDelta] = {}
            reported_error = False  # 是否已在异常分支中向用户报告过具体错误
            for retry_attempt in range(ADAPTIVE_MAX_RETRIES + 1):
                try:
                    # 构建消息列表（重试时可能截断历史）
                    if retry_attempt == 0:
                        messages = [LLMMessage(role="system", content=system_prompt)] + self.history
                    elif retry_attempt == 1:
                        reduced = max(1024, int(current_max_tokens * 0.6))
                        self.llm.max_tokens = reduced
                        messages = [LLMMessage(role="system", content=system_prompt)] + self.history
                        yield {"type": "message", "content": f"\n[系统] 请求异常，缩减 max_tokens 至 {reduced} 重试...\n"}
                    else:
                        reduced = max(512, int(current_max_tokens * 0.3))
                        self.llm.max_tokens = reduced
                        # 使用ContextManager进行智能压缩
                        compressed_history = self.context_manager.compress(self.history)
                        messages = [LLMMessage(role="system", content=system_prompt)] + compressed_history
                        yield {"type": "message", "content": f"\n[系统] 再次异常，智能压缩上下文 + 缩减至 {reduced} tokens 重试...\n"}

                    # 重试时重置当前轮的推理内容（LLM 会重新发送累积推理）
                    if retry_attempt > 0:
                        turn_reasoning = ""

                    tools = self.registry.get_definitions()
                    response = LLMResponse()
                    tool_deltas = {}

                    async for chunk in self.llm.chat_stream_generator(messages, tools=tools):
                        if chunk.reasoning_content:
                            # API 返回的是增量 delta，直接使用即可
                            response.reasoning_content += chunk.reasoning_content
                            turn_reasoning += chunk.reasoning_content
                            has_native_reasoning = True
                            yield {"type": "thinking", "content": chunk.reasoning_content}

                        if chunk.content:
                            response.content += chunk.content
                            if has_native_reasoning:
                                # content 是增量的，直接 yield（不需要 delta 计算）
                                yield {"type": "message", "content": chunk.content}
                            else:
                                for event in process_chunk(chunk.content):
                                    yield event

                        if chunk.tool_call_delta:
                            td = chunk.tool_call_delta
                            if td.index not in tool_deltas:
                                tool_deltas[td.index] = ToolCallDelta(index=td.index)
                            cur = tool_deltas[td.index]
                            if td.id: cur.id = td.id
                            if td.name: cur.name = td.name
                            if td.arguments: cur.arguments += td.arguments

                        if chunk.finish_reason:
                            response.finish_reason = chunk.finish_reason

                    # 成功 — 恢复 max_tokens
                    self.llm.max_tokens = current_max_tokens
                    final_reasoning = turn_reasoning  # 取最后成功轮次的推理
                    break

                except httpx.HTTPStatusError as e:
                    self.llm.max_tokens = current_max_tokens
                    status = e.response.status_code
                    # 尝试提取 API 返回的错误详情
                    try:
                        error_body = e.response.text[:500]
                    except Exception:
                        error_body = "(无法读取响应体)"
                    if status == 429 and retry_attempt < ADAPTIVE_MAX_RETRIES:
                        wait = 4 * (2 ** retry_attempt)
                        yield {"type": "message", "content": f"\n[系统] API 限流(429)，{wait}s 后退避重试...\n"}
                        await asyncio.sleep(wait)
                        continue
                    elif status in (502, 503, 504) and retry_attempt < ADAPTIVE_MAX_RETRIES:
                        wait = 4 * (2 ** retry_attempt)
                        yield {"type": "message", "content": f"\n[系统] 服务不可用({status})，{wait}s 后重试...\n"}
                        await asyncio.sleep(wait)
                        continue
                    else:
                        logger.error(f"LLM API 错误 HTTP {status}: {error_body}")
                        yield {"type": "error", "error": f"API 错误 HTTP {status}: {error_body}"}
                        final_status = "failed"
                        reported_error = True
                        break

                except OSError as e:
                    # chat_stream_generator 包装的 HTTP 错误（含响应体），不应重试
                    self.llm.max_tokens = current_max_tokens
                    error_str = str(e)
                    # 如果请求中包含图片，给出更友好的错误提示
                    if image_paths and "400" in error_str:
                        # 检查是否为 image_url 不被支持的特定错误
                        if "image_url" in error_str:
                            yield {
                                "type": "error",
                                "error": (
                                    f"图片发送失败：当前模型 ({self.config.model.name}) 不支持 image_url 多模态内容。\n"
                                    f"API 返回: {error_str}\n\n"
                                    f"解决方法：\n"
                                    f"1. 将配置文件中的 supports_vision 设为 false（禁用图片功能）\n"
                                    f"2. 或切换至真正支持多模态视觉识别的模型"
                                )
                            }
                        else:
                            yield {
                                "type": "error",
                                "error": (
                                    f"图片发送失败，可能原因：\n"
                                    f"1. 当前模型不支持多模态 / 视觉识别\n"
                                    f"2. 图片格式不受支持\n"
                                    f"3. 图片过大超过 API 限制\n"
                                    f"原始错误: {error_str}"
                                )
                            }
                    else:
                        yield {"type": "error", "error": f"API 错误: {e}"}
                    final_status = "failed"
                    reported_error = True
                    break

                except (httpx.ConnectError, httpx.ReadError,
                        httpx.RemoteProtocolError, httpx.ReadTimeout,
                        httpx.ConnectTimeout) as e:
                    self.llm.max_tokens = current_max_tokens
                    if retry_attempt < ADAPTIVE_MAX_RETRIES:
                        wait = 4 * (2 ** retry_attempt)
                        yield {"type": "message", "content": f"\n[系统] 网络异常({type(e).__name__})，{wait}s 后重试...\n"}
                        await asyncio.sleep(wait)
                        continue
                    else:
                        yield {"type": "error", "error": f"网络连接失败(已重试{ADAPTIVE_MAX_RETRIES}次): {type(e).__name__}"}
                        final_status = "failed"
                        reported_error = True
                        break

                except Exception as e:
                    self.llm.max_tokens = current_max_tokens
                    logger.exception(f"LLM 调用未知异常: {e}")
                    if retry_attempt < ADAPTIVE_MAX_RETRIES:
                        yield {"type": "message", "content": f"\n[系统] 异常({type(e).__name__}): {e}，调整参数自适应重试 (attempt {retry_attempt + 1})...\n"}
                        await asyncio.sleep(1)
                        continue
                    else:
                        yield {"type": "error", "error": f"对话异常(已重试{ADAPTIVE_MAX_RETRIES}次): {type(e).__name__}: {e}"}
                        final_status = "failed"
                        reported_error = True
                        break

            # 如果重试全部失败（无内容），且尚未报告过具体错误，跳到 done
            if (not reported_error and not response.content
                    and not response.tool_calls and not response.reasoning_content):
                yield {"type": "error", "error": (
                    f"LLM 请求多次重试后仍失败 (已重试{ADAPTIVE_MAX_RETRIES}次)。"
                    "请检查网络连接、API 配置或降低任务复杂度后重试。"
                )}
                final_status = "failed"
                break

            # 组装完整 tool calls
            for delta in sorted(tool_deltas.values(), key=lambda d: d.index):
                if delta.name:
                    response.tool_calls.append(FunctionCall(
                        id=delta.id or "", name=delta.name, arguments=delta.arguments,
                    ))

            self.history.append(response.to_message())

            if not response.tool_calls:
                break

            turns += 1
            for tc in response.tool_calls:
                # ---- 先解析参数（所有分支都需要） ----
                try:
                    args = json.loads(tc.arguments) if isinstance(tc.arguments, str) else tc.arguments
                except (json.JSONDecodeError, TypeError) as e:
                    tool_result = ToolResult(
                        tool_call_id=tc.id, name=tc.name, success=False,
                        output="", error=f"参数解析失败: {e}"
                    )
                    self.history.append(tool_result.to_message())
                    yield {
                        "type": "tool_result", "name": tc.name,
                        "success": False, "output": f"参数解析失败: {e}",
                    }
                    continue

                tool = self.registry.get(tc.name)

                # ==== ask_user: 暂停 Agent，等待用户回复 ====
                if tc.name == "ask_user":
                    yield {
                        "type": "ask_user",
                        "name": tc.name,
                        "question": args.get("question", ""),
                        "question_type": args.get("question_type", "text"),
                        "header": args.get("header", ""),
                        "options": args.get("options", []),
                        "multi_select": args.get("multi_select", False),
                    }
                    user_reply = await self.wait_for_user_response(timeout=self.ask_user_timeout)

                    # 取消检查点: 如果等待因取消而唤醒，终止当前任务
                    if self._cancel_event.is_set():
                        tool_result = ToolResult(
                            tool_call_id=tc.id, name=tc.name, success=False,
                            output="", error="任务已被用户取消"
                        )
                        self.history.append(tool_result.to_message())
                        yield {
                            "type": "tool_result", "name": tc.name,
                            "success": False, "output": "任务已被用户取消",
                        }
                        continue

                    if user_reply.get("status") == "timeout":
                        self._ask_user_timeout_count += 1
                        timeout_minutes = int(self.ask_user_timeout / 60)
                        # 连续超时 3 次，自动终止任务
                        if self._ask_user_timeout_count >= 3:
                            yield {
                                "type": "message",
                                "content": (
                                    f"\n⚠️ 用户连续 {self._ask_user_timeout_count} 次未回复，任务自动终止。\n"
                                ),
                            }
                            final_status = "timeout"
                            # 跳出整个 while turns 循环
                            tool_result = ToolResult(
                                tool_call_id=tc.id, name=tc.name, success=False,
                                output="", error=f"用户连续{self._ask_user_timeout_count}次未回复，任务已终止"
                            )
                            self.history.append(tool_result.to_message())
                            yield {
                                "type": "tool_result", "name": tc.name,
                                "success": False, "output": f"用户连续{self._ask_user_timeout_count}次未回复，任务已终止",
                            }
                            # 跳过后续处理，直接结束对话
                            turns = self.max_turns  # 强制跳出外层 while 循环
                            break
                        tool_result = ToolResult(
                            tool_call_id=tc.id, name=tc.name, success=False,
                            output="", error=f"用户回复超时 ({timeout_minutes}分钟)"
                        )
                    else:
                        self._ask_user_timeout_count = 0  # 成功回复，重置超时计数
                        tool_result = ToolResult(
                            tool_call_id=tc.id, name=tc.name, success=True,
                            output=f"用户回复: {user_reply.get('answer', user_reply.get('choice', ''))}",
                        )

                    self.history.append(tool_result.to_message())
                    yield {
                        "type": "tool_result", "name": tc.name,
                        "success": tool_result.success,
                        "output": (tool_result.output or "")[:500],
                    }
                    continue

                custom_msg = ""

                # ==== require_confirm: 暂停等待用户审批 (auto_approve / auto_confirm_tools 模式跳过) ====
                if tool and tool.require_confirm and not self._should_auto_confirm(tc.name):
                    # 构建确认信息 — 提取代表性的预览字段
                    preview = (
                        args.get("command")
                        or args.get("file_path")
                        or args.get("question")
                        or str(list(args.keys()))
                    )
                    yield {
                        "type": "need_confirm",
                        "name": tc.name,
                        "arguments": str(args)[:500],
                        "output_preview": str(preview)[:200],
                    }
                    user_reply = await self.wait_for_user_response(timeout=self.ask_user_timeout)
                    if self._cancel_event.is_set():
                        tool_result = ToolResult(
                            tool_call_id=tc.id, name=tc.name, success=False,
                            output="", error="任务已被用户取消"
                        )
                        self.history.append(tool_result.to_message())
                        yield {
                            "type": "tool_result", "name": tc.name,
                            "success": False, "output": "任务已被用户取消",
                        }
                        continue

                    approved = user_reply.get("approved", False)
                    custom_msg = user_reply.get("custom_message", "")
                    if not approved:
                        tool_result = ToolResult(
                            tool_call_id=tc.id, name=tc.name, success=False,
                            output="⏭ 用户跳过执行", error="用户拒绝执行",
                        )
                        self.history.append(tool_result.to_message())
                        yield {
                            "type": "tool_result", "name": tc.name,
                            "success": False, "output": "用户跳过执行",
                        }
                        continue

                # ==== 正常执行工具 ====
                yield {"type": "tool_call", "name": tc.name, "arguments": tc.arguments}

                # 回滚追踪：写文件前备份原内容、shell 命令标记
                self._track_rollback(tc.name, tool, args)

                tool_result: ToolResult
                if tool:
                    try:
                        # 后台执行工具，期间每秒发送 tool_progress 事件
                        # 这样 CLI 可以显示旋转进度 + 耗时，防止用户以为卡死
                        tool_task = asyncio.ensure_future(tool.execute(**args))
                        start_time = asyncio.get_event_loop().time()
                        while not tool_task.done():
                            try:
                                await asyncio.wait_for(asyncio.shield(tool_task), timeout=1.0)
                            except asyncio.TimeoutError:
                                elapsed = int(asyncio.get_event_loop().time() - start_time)
                                yield {
                                    "type": "tool_progress",
                                    "name": tc.name,
                                    "elapsed": elapsed,
                                }
                            # 取消检查点: 用户请求取消时终止工具执行
                            if self._cancel_event.is_set():
                                # 调用工具的 cancel() 方法（如 kill 子进程）
                                try:
                                    await tool.cancel()
                                except Exception:
                                    pass
                                tool_task.cancel()
                                try:
                                    await tool_task
                                except (asyncio.CancelledError, Exception):
                                    pass
                                tool_result = ToolResult(
                                    tool_call_id=tc.id, name=tc.name, success=False,
                                    output="", error="任务已被用户取消"
                                )
                                break
                        else:
                            # 工具正常完成（未被取消）
                            tool_result = tool_task.result()
                    except Exception as e:
                        tool_result = ToolResult(
                            tool_call_id=tc.id, name=tc.name, success=False,
                            output="", error=f"工具执行异常: {e}"
                        )
                else:
                    tool_result = ToolResult(
                        tool_call_id=tc.id, name=tc.name, success=False,
                        output="", error=f"未知工具: {tc.name}"
                    )
                # 如果有用户备注（need_confirm 附带说明），注入到输出中
                if custom_msg:
                    tool_result.output = f"[用户备注: {custom_msg}]\n{tool_result.output or ''}"
                # 工具返回的 ToolResult.tool_call_id 可能为空，用 LLM 返回的 id 覆盖
                tool_result.tool_call_id = tc.id
                tool_result.name = tc.name
                self.history.append(tool_result.to_message())
                yield {
                    "type": "tool_result",
                    "name": tc.name,
                    "success": tool_result.success,
                    "output": (tool_result.output or "")[:500],
                }

        # 清空残留缓冲区
        if not has_native_reasoning:
            if in_thinking and thinking_buf:
                yield {"type": "thinking", "content": thinking_buf}
            if content_buf:
                yield {"type": "message", "content": content_buf}

        # ---- 检测是否达到轮次上限 ----
        if turns >= self.max_turns and response.tool_calls:
            yield {
                "type": "message",
                "content": (
                    f"\n⚠️ 已达到最大交互轮次 ({self.max_turns})，任务自动终止。\n"
                    "建议：将复杂任务拆分为多个子任务分步执行，"
                    "或在 .kfzcode.json 中增大 behavior.max_turns 配置。\n"
                ),
            }
            final_status = "max_turns"

        # 提取最终 thinking
        thinking_content = final_reasoning.strip()
        final_content = response.content.strip() if response.content else ""

        if not has_native_reasoning and not thinking_content and "<thinking>" in final_content and "</thinking>" in final_content:
            start = final_content.index("<thinking>") + len("<thinking>")
            end = final_content.index("</thinking>")
            thinking_content = final_content[start:end].strip()
            final_content = final_content[end + len("</thinking>"):].strip()

        if has_native_reasoning and "<thinking>" in final_content and "</thinking>" in final_content:
            end = final_content.index("</thinking>") + len("</thinking>")
            final_content = final_content[end:].strip()

        yield {
            "type": "done",
            "status": final_status,
            "thinking": thinking_content,
            "content": final_content,
        }
        
        # 保存历史记录到持久化存储
        self._save_history()

        # 任务结束（成功/失败/超时等非取消结局）：丢弃备份，不回滚
        self.task_ctx.clear_backups()

    async def close(self) -> None:
        # 保存历史记录到持久化存储
        self._save_history()
        await self.llm.close()
