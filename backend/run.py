"""KFZCode backend startup/stop/restart script."""
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
PID_FILE = BASE_DIR / ".pid"
LOCK_FILE = BASE_DIR / ".startup.lock"
LOG_FILE = BASE_DIR / "backend.log"
# 命令行唯一标记，用于全盘扫描时精准定位本项目的后端进程
MARKER = "--kfzcode-server"
PORT = 8765


def read_pid() -> int | None:
    if not PID_FILE.exists():
        return None
    try:
        return int(PID_FILE.read_text().strip())
    except (ValueError, OSError):
        return None


def is_running(pid: int) -> bool:
    """检查进程是否仍在运行（跨平台）。

    注意：Windows 上 os.kill(pid, 0) 在进程被杀后的一段时间内
    仍可能不抛异常（WinError 87 vs 进程残留的竞态），因此改用
    WMIC 查询进程列表来精确验证。
    """
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["wmic", "process", "where", f"ProcessId={pid}",
                 "get", "ProcessId", "/format:csv"],
                capture_output=True, text=True, timeout=5,
            )
            return str(pid) in result.stdout
        except Exception:
            # WMIC 不可用时回退到 os.kill（不够精确，但好过什么都不做）
            try:
                os.kill(pid, 0)
                return True
            except OSError:
                return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False


def _find_kfzcode_pids() -> set[int]:
    """全盘扫描：找出所有 KFZCode 后端相关进程的 PID。

    策略：
    1. 扫描所有 Python 进程的命令行，匹配唯一标记 --kfzcode-server
    2. 扫描占用目标端口的进程（兜底）
    """
    pids = set()

    # 方法 1：按命令行匹配（最可靠）
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["wmic", "process", "where", "name=\"python.exe\"",
                 "get", "ProcessId,CommandLine", "/format:csv"],
                capture_output=True, text=True, timeout=10,
            )
            for line in result.stdout.splitlines():
                lower = line.lower()
                if MARKER in lower:
                    parts = line.strip().split(",")
                    if parts:
                        try:
                            pid = int(parts[-1].strip())
                            if pid and pid != os.getpid():
                                pids.add(pid)
                        except ValueError:
                            pass
        except Exception:
            pass
    else:
        try:
            result = subprocess.run(
                ["pgrep", "-f", MARKER],
                capture_output=True, text=True, timeout=5,
            )
            for line in result.stdout.strip().splitlines():
                try:
                    pid = int(line.strip())
                    if pid != os.getpid():
                        pids.add(pid)
                except ValueError:
                    pass
        except Exception:
            pass

    # 方法 2：端口扫描兜底
    try:
        if os.name == "nt":
            result = subprocess.run(
                ["netstat", "-ano", "-p", "TCP"],
                capture_output=True, text=True, timeout=5,
            )
            for line in result.stdout.splitlines():
                if f":{PORT}" in line and "LISTENING" in line:
                    parts = line.split()
                    if parts:
                        try:
                            pid = int(parts[-1])
                            if pid != os.getpid():
                                pids.add(pid)
                        except ValueError:
                            pass
        else:
            result = subprocess.run(
                ["lsof", "-ti", f":{PORT}"],
                capture_output=True, text=True, timeout=5,
            )
            for line in result.stdout.strip().splitlines():
                try:
                    pid = int(line.strip())
                    if pid != os.getpid():
                        pids.add(pid)
                except ValueError:
                    pass
    except Exception:
        pass

    return pids


def save_pid(pid: int):
    PID_FILE.write_text(str(pid))


def remove_pid():
    if PID_FILE.exists():
        PID_FILE.unlink(missing_ok=True)


def kill_process(pid: int) -> bool:
    """跨平台杀进程，成功后验证进程确实已死。"""
    if not is_running(pid):
        return True

    if os.name == "nt":
        try:
            result = subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True, timeout=10,
            )
            # 把 taskkill 的错误信息透出来，方便排查
            stderr = result.stderr.decode(errors="replace").strip() if result.stderr else ""
            if result.returncode != 0 and stderr:
                print(f"  {stderr}")
        except Exception as e:
            print(f"  taskkill exception: {e}")
        time.sleep(0.5)
        return not is_running(pid)
    else:
        try:
            os.kill(pid, signal.SIGTERM)
            for _ in range(10):
                time.sleep(0.1)
                if not is_running(pid):
                    return True
            os.kill(pid, signal.SIGKILL)
            time.sleep(0.3)
            return not is_running(pid)
        except OSError:
            return False


def _acquire_lock() -> bool:
    """获取启动互斥锁（原子操作，防止同时打开两个 start）。

    使用 os.O_EXCL 确保文件创建的原子性，跨平台有效。
    如果锁文件的持有者进程已死，自动接管（清理僵尸锁）。
    """
    try:
        fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_RDWR)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        # 锁已存在 — 检查持有者是否还活着
        try:
            locker_pid = int(LOCK_FILE.read_text().strip())
            if not is_running(locker_pid):
                # 僵尸锁，接管
                LOCK_FILE.unlink(missing_ok=True)
                fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_RDWR)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                return True
        except (ValueError, OSError):
            # 锁文件损坏，强制删除后重建
            LOCK_FILE.unlink(missing_ok=True)
            try:
                fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_RDWR)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                return True
            except FileExistsError:
                pass
        return False


def _release_lock():
    """释放启动互斥锁。"""
    try:
        LOCK_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def start():
    # 互斥锁：阻止同时启动两个后端
    if not _acquire_lock():
        print("[ERROR] Another start operation is already in progress.")
        print(f"Wait for it to complete, or delete {LOCK_FILE} if stale.")
        return 1

    try:
        # 先停止任何已存在的后端进程（包括用其他方式启动的），「启动」即「重启」
        _stop_existing()

        remove_pid()

        print("=" * 40)
        print("  KFZCode Backend Starting...")
        print("=" * 40)
        print()

        proc = subprocess.Popen(
            [sys.executable, "-m", "src.main", MARKER],
            cwd=str(BASE_DIR),
            stdout=open(LOG_FILE, "w", encoding="utf-8"),
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )

        save_pid(proc.pid)
        print(f"Backend started (PID: {proc.pid})")
        print(f"Log file: {LOG_FILE}")
        print(f"URL:      http://localhost:{PORT}")
        print(f"Health:   http://localhost:{PORT}/api/health")
        print()
        print("Run stop.bat to stop the backend.")
        return 0
    finally:
        _release_lock()


def _stop_existing():
    """停止所有已存在的 KFZCode 后端进程。"""
    pids = _find_kfzcode_pids()
    if not pids:
        return

    print(f"[INFO] Found {len(pids)} existing backend process(es): {sorted(pids)}")
    for pid in sorted(pids):
        print(f"[INFO] Stopping PID {pid}...")
        if kill_process(pid):
            print(f"[INFO]   -> Stopped.")
        else:
            print(f"[WARNING]   -> Failed to kill PID {pid}, may need administrator.")

    time.sleep(0.5)


def stop():
    """停止后端：全盘扫描所有 KFZCode 相关进程并杀掉。"""
    # 1. 全盘扫描并杀进程
    pids = _find_kfzcode_pids()

    if not pids:
        print("No KFZCode backend processes found.")
        remove_pid()
        _release_lock()
        print("Done.")
        return 0

    print(f"Found {len(pids)} backend process(es): {sorted(pids)}")
    failed = []
    for pid in sorted(pids):
        print(f"Stopping PID {pid}...")
        if kill_process(pid):
            print(f"  -> Stopped.")
        else:
            print(f"  -> [ERROR] Failed. Try running as Administrator.")
            failed.append(pid)

    if failed:
        print(f"[WARNING] {len(failed)} process(es) could not be stopped: {failed}")

    # 2. 清理残留文件
    remove_pid()
    _release_lock()
    print("Done.")
    return 0


def restart():
    print("=" * 40)
    print("  Restarting KFZCode Backend")
    print("=" * 40)
    print()

    print("[1/2] Stopping...")
    stop()
    print()

    print("[2/2] Starting...")
    return start()


def main():
    if len(sys.argv) < 2:
        print("Usage: python run.py [start|stop|restart]")
        return 1

    command = sys.argv[1].lower()
    if command == "start":
        return start()
    elif command == "stop":
        return stop()
    elif command == "restart":
        return restart()
    else:
        print(f"Unknown command: {command}")
        print("Usage: python run.py [start|stop|restart]")
        return 1


if __name__ == "__main__":
    sys.exit(main())
