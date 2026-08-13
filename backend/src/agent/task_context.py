"""任务上下文 — 多 Agent 模式的 per-task 状态隔离

将「取消标志」「Human-in-the-Loop 等待」「文件回滚追踪」从全局单例中剥离，
改为每个任务一份，避免多任务串扰，并支持取消时回滚本任务的文件修改。
"""
import asyncio
from dataclasses import dataclass, field


# 外部用户回复（ask_user / need_confirm）默认等待超时（秒），默认 2 小时
USER_RESPONSE_TIMEOUT = 7200.0


@dataclass
class TaskContext:
    """单个任务的独立状态"""

    task_id: str
    session_id: str = ""
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    user_response_event: asyncio.Event | None = None
    user_response_data: dict = field(default_factory=dict)

    # ==== 回滚追踪（任务取消时回滚本任务的文件修改） ====
    file_backups: dict[str, bytes] = field(default_factory=dict)  # 路径 -> 原始内容
    created_files: set[str] = field(default_factory=set)          # 本任务新建的文件
    shell_used: bool = False                                       # 是否执行过 shell（触发 git 兜底）
    git_head: str = ""                                             # 任务开始时的 git HEAD
    git_dirty_before: set[str] = field(default_factory=set)       # 任务开始前用户已有改动

    async def wait_for_user_response(self, timeout: float = USER_RESPONSE_TIMEOUT) -> dict:
        """阻塞等待外部用户回复，直到 respond_user() 被调用或超时。

        Returns:
            用户回复数据字典；超时时返回 {"status": "timeout"}
        """
        self.user_response_data = {}
        self.user_response_event = asyncio.Event()
        try:
            await asyncio.wait_for(self.user_response_event.wait(), timeout=timeout)
            return self.user_response_data
        except asyncio.TimeoutError:
            return {"status": "timeout"}
        finally:
            self.user_response_event = None

    def respond_user(self, data: dict) -> None:
        """接收外部用户回复，唤醒正在等待的 Agent。"""
        if self.user_response_event is None:
            return
        self.user_response_data = data
        self.user_response_event.set()

    def cancel(self) -> None:
        """设置取消标志，并唤醒正在等待用户回复的 Agent。"""
        self.cancel_event.set()
        if self.user_response_event is not None:
            self.user_response_data = {"status": "cancelled"}
            self.user_response_event.set()

    def is_cancelled(self) -> bool:
        """返回当前任务是否已被取消。"""
        return self.cancel_event.is_set()

    # ==== 回滚追踪辅助方法 ====
    def backup_file(self, path: str, content: bytes) -> None:
        """备份文件原始内容（仅首次备份，保留改动前的最初状态）。"""
        if path not in self.file_backups:
            self.file_backups[path] = content

    def mark_created(self, path: str) -> None:
        """标记本任务新建的文件（回滚时删除）。"""
        self.created_files.add(path)

    def mark_shell_used(self) -> None:
        """标记本任务执行过 shell 命令（回滚时触发 git 兜底）。"""
        self.shell_used = True

    def clear_backups(self) -> None:
        """清理所有备份数据（任务结束时调用，防止内存泄漏）。"""
        self.file_backups.clear()
        self.created_files.clear()
        self.shell_used = False
        self.git_head = ""
        self.git_dirty_before.clear()
