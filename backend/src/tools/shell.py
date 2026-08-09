"""Shell 命令执行工具"""
import asyncio
import os
import shlex
from pathlib import Path

from .base import ToolBase
from ..llm.types import ToolResult


class ShellTool(ToolBase):
    name = "execute_command"
    description = "执行 Shell 命令并返回结果。支持 npm/pip/git/python 等常见开发命令。你拥有完整的命令执行能力，可以直接运行测试、构建、安装依赖等操作。"
    parameters = {
        "command": {
            "type": "string",
            "description": "要执行的完整 Shell 命令",
        },
        "working_dir": {
            "type": "string",
            "description": "工作目录（默认项目根目录）",
            "default": ".",
        },
        "timeout": {
            "type": "integer",
            "description": "超时时间（秒）",
            "default": 120,
        },
    }
    require_confirm = True  # 执行命令需要确认

    # 危险命令模式
    DANGEROUS_PATTERNS = [
        "rm -rf /", "rm -rf /*", "mkfs.", "dd if=",
        ":(){ :|:& };:",  # fork bomb
        "> /dev/sda", "shutdown", "reboot", "halt",
        "chmod 777 /", "chown -R /",
    ]

    # 自保模式 — 命令包含这些关键词时会触发额外自保检查
    # 保护范围: 后端 Python 进程 AND CLI Node.js 进程
    SELF_PRESERVATION_PATTERNS = [
        "taskkill", "kill", "pkill", "killall",
        "tskill", "wmic process", "stop-process",
    ]

    # 受保护的可执行文件名 — 禁止通过 IM/NAME 批量终止这些进程
    PROTECTED_EXECUTABLES = ["python", "python.exe", "python3",
                              "node", "node.exe", "nodejs"]

    # 服务器启动标志 — 检测到这些输出说明服务已成功启动
    SERVER_START_PATTERNS = [
        "Running on ", "Serving on ", "server started",
        "Server started", "Listening on", "listening on",
        "http://", "Running in production", "Development server",
        "Starting server", "started successfully",
    ]

    # 这些命令可能启动长时间服务，需要用特殊策略
    SERVER_COMMAND_KEYWORDS = [
        "run", "serve", "start", "python ", "node ",
        "flask", "gunicorn", "uvicorn", "django",
        "npm start", "npm run", "yarn start", "yarn run",
    ]

    def __init__(self, workspace_root: str,
                 allowed_commands: list[str] | None = None,
                 deny_commands: list[str] | None = None):
        self.workspace_root = Path(workspace_root).resolve()
        self.allowed_commands = allowed_commands or []
        self.deny_commands = deny_commands or []
        self._current_process: asyncio.subprocess.Process | None = None

    def _is_server_command(self, command: str) -> bool:
        """检测命令是否可能是启动服务器的（需要特殊处理）"""
        cmd_lower = command.lower()
        return any(kw in cmd_lower for kw in self.SERVER_COMMAND_KEYWORDS)

    async def execute(self, command: str, working_dir: str = ".",
                      timeout: int = 120) -> ToolResult:
        # 安全检查
        is_dangerous, reason = self._check_dangerous(command)
        if is_dangerous:
            return ToolResult(
                tool_call_id="", name=self.name, success=False,
                output="", error=f"命令被拒绝: {reason}"
            )

        try:
            cwd = self._resolve_dir(working_dir)
            cwd.mkdir(parents=True, exist_ok=True)

            # 检测是否为服务启动命令
            is_server = self._is_server_command(command)
            if is_server:
                # 服务命令：缩短 gather_timeout，检测启动输出后提前结束
                return await self._execute_server_command(command, cwd, timeout)
            else:
                return await self._execute_normal_command(command, cwd, timeout)

        except Exception as e:
            return ToolResult(
                tool_call_id="", name=self.name, success=False,
                output="", error=str(e)
            )

    async def cancel(self) -> None:
        """终止当前正在运行的子进程"""
        proc = self._current_process
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass

    async def _execute_with_communicate_timeout(
        self, proc: asyncio.subprocess.Process, timeout: int,
    ) -> tuple[bytes | None, bytes | None, bool]:
        """可可靠地在 Windows 上超时 proc.communicate()

        不要用 asyncio.wait_for(proc.communicate(), timeout=N) —
        Windows ProactorEventLoop 无法可靠取消底层 I/O，
        导致 communicate() 永久阻塞。
        改用 asyncio.wait(timeout=N) 纯计时器 + proc.kill() 事后清理。
        """
        self._current_process = proc
        try:
            comm_task = asyncio.ensure_future(proc.communicate())
            done, pending = await asyncio.wait([comm_task], timeout=timeout)

            if comm_task in pending:
                # 超时 — 暴力终止进程
                timed_out = True
                try:
                    proc.kill()
                except Exception:
                    pass
                # communicate 应在进程被 kill 后很快完成（管道关闭）
                try:
                    stdout, stderr = await asyncio.wait_for(comm_task, timeout=3.0)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    comm_task.cancel()
                    stdout, stderr = b"", b""
            else:
                timed_out = False
                stdout, stderr = comm_task.result()

            return stdout or b"", stderr or b"", timed_out
        finally:
            self._current_process = None

    async def _execute_normal_command(self, command: str, cwd: Path,
                                       timeout: int) -> ToolResult:
        """执行普通命令（非服务类），等待完整输出"""
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd),
        )

        stdout, stderr, timed_out = await self._execute_with_communicate_timeout(
            proc, timeout,
        )

        if timed_out:
            await proc.wait()
            return ToolResult(
                tool_call_id="", name=self.name, success=False,
                output="", error=f"命令执行超时 ({timeout}s): {command}"
            )

        stdout_bytes = stdout if isinstance(stdout, bytes) else b""
        stderr_bytes = stderr if isinstance(stderr, bytes) else b""
        stdout_text = stdout_bytes.decode("utf-8", errors="replace")[:8000]
        stderr_text = stderr_bytes.decode("utf-8", errors="replace")[:4000]

        output = f"$ {command}\n"
        if stdout_text:
            output += stdout_text
        if stderr_text:
            output += f"\n[stderr]\n{stderr_text}"
        output += f"\n[退出码: {proc.returncode}]"

        success = proc.returncode == 0
        return ToolResult(
            tool_call_id="", name=self.name, success=success,
            output=output.strip(),
            error=None if success else f"命令以非零退出码 {proc.returncode} 结束"
        )

    async def _execute_server_command(self, command: str, cwd: Path,
                                       timeout: int) -> ToolResult:
        """执行服务启动命令（如 Flask/Django/npm start）

        策略：服务命令通常会持续运行。使用短超时 collect 初始输出，
        如果进程仍存活 → 说明服务启动成功，kill 并返回 success。
        如果进程自行退出 → 返回实际退出结果。
        """
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(cwd),
        )

        # 服务启动等待时间：较短，因为只需要确认进程没有立即崩溃
        wait_timeout = min(timeout, 15)

        stdout, _, timed_out = await self._execute_with_communicate_timeout(
            proc, wait_timeout,
        )

        if timed_out:
            # 进程在超时后仍存活 → 服务启动成功
            try:
                await proc.wait()
            except Exception:
                pass

            stdout_bytes = stdout if isinstance(stdout, bytes) else b""
            output_text = stdout_bytes.decode("utf-8", errors="replace")[:8000]

            output = f"$ {command}\n{output_text}\n\n[✓ 服务已在后台启动 (进程在 {wait_timeout}s 后仍运行)]"
            return ToolResult(
                tool_call_id="", name=self.name, success=True,
                output=output.strip(),
            )
        else:
            # 进程在超时前自行退出
            stdout_bytes = stdout if isinstance(stdout, bytes) else b""
            output_text = stdout_bytes.decode("utf-8", errors="replace")[:8000]
            output = f"$ {command}\n{output_text}"
            if proc.returncode == 0:
                output += "\n[进程已正常退出]"
                return ToolResult(
                    tool_call_id="", name=self.name, success=True,
                    output=output.strip(),
                )
            else:
                output += f"\n[退出码: {proc.returncode}]"
                return ToolResult(
                    tool_call_id="", name=self.name, success=False,
                    output=output.strip(),
                    error=f"命令以非零退出码 {proc.returncode} 结束"
                )

    def _check_dangerous(self, command: str) -> tuple[bool, str]:
        """检查命令是否危险"""
        cmd_lower = command.lower().strip()

        # 检查禁止名单
        for pattern in self.deny_commands:
            if pattern.lower() in cmd_lower:
                return True, f"匹配禁止模式: {pattern}"

        # 检查危险模式
        for pattern in self.DANGEROUS_PATTERNS:
            if pattern.lower() in cmd_lower:
                return True, f"匹配危险模式: {pattern}"

        # 如果有白名单，检查命令是否在白名单内
        if self.allowed_commands:
            cmd_base = cmd_lower.split()[0] if cmd_lower.split() else ""
            if cmd_base not in [c.lower() for c in self.allowed_commands]:
                return True, f"命令不在白名单内: {cmd_base}"

        # 自保检查：防止命令杀掉后端进程自身
        self_preserve_reason = self._check_self_preservation(command)
        if self_preserve_reason:
            return True, self_preserve_reason

        return False, ""

    def _check_self_preservation(self, command: str) -> str:
        """检查命令是否会杀掉 KFZCode 自身的进程（自保检查）

        保护范围:
        - 后端 Python 进程 (python.exe / python3)
        - CLI Node.js 进程 (node.exe) — 防止 taskkill /f /im node.exe 误杀

        检测以下危险模式：
        1. taskkill /F /IM python.exe|node.exe — 批量杀进程（含 KFZCode 自身）
        2. taskkill /PID <当前PID> — 直接杀自身
        3. pkill/killall python|node — 批量杀进程
        4. wmic / Stop-Process 删除 python|node 进程
        5. kill / powershell 命令中含当前进程 PID

        Returns:
            非空字符串 = 拒绝原因；空字符串 = 安全
        """
        cmd_lower = command.lower().strip()
        current_pid = os.getpid()

        # 命令中是否包含杀进程关键词
        has_kill_keyword = any(kw in cmd_lower for kw in self.SELF_PRESERVATION_PATTERNS)

        if not has_kill_keyword:
            return ""

        # 检查 1: 批量杀进程 (taskkill /F /IM <exe>, pkill <name>, etc.)
        for exe in self.PROTECTED_EXECUTABLES:
            if exe in cmd_lower:
                # taskkill /im xxx.exe 或 taskkill /F /IM xxx.exe
                if "taskkill" in cmd_lower or "tskill" in cmd_lower:
                    return (
                        f"命令被拒绝: 禁止使用 taskkill 批量终止 {exe} 进程"
                        "（会杀掉 KFZCode 自身进程），请使用 netstat -ano | findstr 查找"
                        "特定端口对应的 PID，再用 taskkill /PID <pid> 精确终止"
                    )
                # pkill xxx / killall xxx
                if "pkill" in cmd_lower or "killall" in cmd_lower:
                    return f"命令被拒绝: 禁止使用 pkill/killall 批量终止 {exe} 进程（会杀掉 KFZCode 自身）"
                # wmic process where name="xxx.exe" delete
                if "wmic" in cmd_lower and "delete" in cmd_lower:
                    return f"命令被拒绝: 禁止通过 WMIC 删除 {exe} 进程（会杀掉 KFZCode 自身）"
                # PowerShell Stop-Process -Name xxx
                if "stop-process" in cmd_lower:
                    return f"命令被拒绝: 禁止使用 Stop-Process 批量终止 {exe} 进程（会杀掉 KFZCode 自身）"

        # 检查 2: 命令中包含当前后端进程 PID (taskkill /PID <current>, kill -9 <current>)
        if str(current_pid) in cmd_lower:
            return (
                f"命令被拒绝: 命令中包含当前后端进程 PID ({current_pid})，"
                "执行将导致后端自身终止"
            )

        return ""

    def _resolve_dir(self, directory: str) -> Path:
        path = Path(directory)
        if not path.is_absolute():
            path = self.workspace_root / path
        return path.resolve()
