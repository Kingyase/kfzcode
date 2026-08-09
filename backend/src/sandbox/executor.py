"""沙箱执行器 — 子进程隔离"""
import asyncio
from pathlib import Path


class SandboxExecutor:
    """在受限子进程中执行命令"""

    def __init__(self, workspace_root: str, timeout: int = 120):
        self.workspace_root = Path(workspace_root).resolve()
        self.timeout = timeout

    async def execute(self, command: str, cwd: str = "",
                      env: dict | None = None) -> dict:
        """执行命令并返回结果"""
        work_dir = Path(cwd) if cwd and Path(cwd).is_absolute() else self.workspace_root / (cwd or ".")
        work_dir = work_dir.resolve()
        work_dir.mkdir(parents=True, exist_ok=True)

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(work_dir),
                env=env,
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=self.timeout
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return {
                    "success": False,
                    "stdout": "",
                    "stderr": f"命令超时 ({self.timeout}s)",
                    "exit_code": -1,
                }

            return {
                "success": proc.returncode == 0,
                "stdout": stdout.decode("utf-8", errors="replace")[:16000],
                "stderr": stderr.decode("utf-8", errors="replace")[:8000],
                "exit_code": proc.returncode,
            }
        except FileNotFoundError:
            return {
                "success": False,
                "stdout": "",
                "stderr": f"命令未找到: {command.split()[0]}",
                "exit_code": -1,
            }
        except Exception as e:
            return {
                "success": False,
                "stdout": "",
                "stderr": str(e),
                "exit_code": -1,
            }
