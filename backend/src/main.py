"""KFZCode FastAPI 服务入口"""
import json
import asyncio
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Dict

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from sse_starlette.sse import EventSourceResponse

from .config import KFZCodeConfig, config_loader, ModelConfig
from .agent.engine import KFZCodeEngine, SingleAgentRunner


# 全局引擎实例
engine: KFZCodeEngine | None = None

# Session管理: session_id -> SingleAgentRunner
session_runners: Dict[str, SingleAgentRunner] = {}
# 每个 session 的最后活动时间戳 (time.monotonic())，用于 TTL 过期清理
_session_last_active: Dict[str, float] = {}
# 项目路径到session_id的映射
project_sessions: Dict[str, str] = {}
# Session 无活动过期时间（秒），默认 2 小时
SESSION_TTL_SECONDS = 2 * 60 * 60
# 定期清理间隔（秒）
_CLEANUP_INTERVAL = 5 * 60
# 后台清理任务句柄
_cleanup_task: asyncio.Task | None = None


async def _close_runner(session_id: str) -> None:
    """安全关闭一个 runner，清理关联的 project_sessions 映射。"""
    runner = session_runners.get(session_id)
    if not runner:
        return
    # 清理 project_sessions 中指向此 session 的条目
    for proj, sid in list(project_sessions.items()):
        if sid == session_id:
            del project_sessions[proj]
    try:
        await runner.close()
    except Exception:
        pass
    session_runners.pop(session_id, None)
    _session_last_active.pop(session_id, None)


async def _cleanup_expired_sessions() -> None:
    """后台任务：定期清理超过 TTL 未活动的 session runner。"""
    while True:
        try:
            await asyncio.sleep(_CLEANUP_INTERVAL)
            now = time.monotonic()
            expired = [
                sid for sid, ts in list(_session_last_active.items())
                if now - ts > SESSION_TTL_SECONDS
            ]
            for sid in expired:
                await _close_runner(sid)
            if expired:
                print(f"[Session] 清理了 {len(expired)} 个过期会话 (TTL={SESSION_TTL_SECONDS}s)")
        except asyncio.CancelledError:
            break
        except Exception:
            pass


def _touch_session(session_id: str) -> None:
    """更新 session 的最后活动时间。"""
    _session_last_active[session_id] = time.monotonic()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用启动/关闭"""
    global engine, _cleanup_task, SESSION_TTL_SECONDS
    config = config_loader.load()
    # 从配置文件读取 session TTL（默认 2 小时）
    SESSION_TTL_SECONDS = config.behavior.session_ttl_seconds
    engine = KFZCodeEngine(config)
    await engine.start()
    # 启动后台 session 过期清理任务
    _cleanup_task = asyncio.create_task(_cleanup_expired_sessions(), name="session-cleanup")
    print(f"KFZCode engine started. Workspace: {engine.workspace}, Session TTL: {SESSION_TTL_SECONDS}s")
    yield
    # 停止后台清理
    if _cleanup_task:
        _cleanup_task.cancel()
        try:
            await _cleanup_task
        except asyncio.CancelledError:
            pass
    # 关闭所有活跃 session
    for sid in list(session_runners.keys()):
        await _close_runner(sid)
    if engine:
        await engine.stop()
    print("KFZCode engine stopped.")


app = FastAPI(
    title="KFZCode API",
    description="KFZCode — 内网 AI 编程助手",
    version="1.0.1",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
async def health_check():
    """健康检查"""
    if not engine:
        return JSONResponse({"status": "not_initialized"}, status_code=503)
    health = await engine.health_check()
    return JSONResponse(health)


# ===== 对话接口 =====

@app.post("/api/chat")
async def chat_single(request: Request):
    """单 Agent 对话 (SSE 流式)"""
    body = await request.json()
    message = body.get("message", "")
    image_paths: list[str] = body.get("images", [])
    project_path = body.get("project_path", str(Path.cwd()))
    profile = body.get("profile")
    model_name = body.get("model")
    session_id = body.get("session_id")
    auto_approve = body.get("auto_approve", False)

    if not message:
        raise HTTPException(status_code=400, detail="message is required")

    # 获取或创建session
    if not session_id:
        # 检查项目路径是否已有活跃session
        if project_path in project_sessions:
            session_id = project_sessions[project_path]
        else:
            session_id = str(uuid.uuid4())

    # 创建或复用runner
    if session_id not in session_runners:
        # 同一项目已有旧 runner 时，关闭旧的防止泄漏
        if project_path in project_sessions:
            old_sid = project_sessions[project_path]
            if old_sid != session_id and old_sid in session_runners:
                await _close_runner(old_sid)
        config = config_loader.load(project_path=project_path, profile=profile, model_name=model_name)
        runner = SingleAgentRunner(config, workspace=project_path, session_id=session_id, auto_approve=auto_approve)
        session_runners[session_id] = runner
        project_sessions[project_path] = session_id
    else:
        runner = session_runners[session_id]

    # 更新最后活动时间
    _touch_session(session_id)

    # 图片附件：检查模型是否支持多模态
    if image_paths:
        config = config_loader.load(project_path=project_path, profile=profile, model_name=model_name)
        if not config.model.supports_vision:
            raise HTTPException(
                status_code=400,
                detail=f"当前模型 {config.model.name} 不支持图片识别，请切换支持多模态的模型"
            )

    # 将 async generator 包装为可 aclose 的引用，供 finally 清理
    run_gen = runner.run(message, image_paths=image_paths if image_paths else None)

    async def event_stream():
        nonlocal run_gen
        try:
            yield {
                "event": "start",
                "data": json.dumps({
                    "status": "started",
                    "session_id": session_id,
                    "has_images": len(image_paths) > 0,
                    "image_count": len(image_paths),
                })
            }

            async for event in run_gen:
                event_type = event.pop("type", "progress")
                yield {"event": event_type, "data": json.dumps(event, ensure_ascii=False)}

                # 每隔一个 yield 检查客户端是否已断开，避免盲发
                if event_type in ("message", "tool_result", "done"):
                    try:
                        if await request.is_disconnected():
                            break
                    except Exception:
                        pass  # is_disconnected() 可能不受支持，忽略

        except asyncio.CancelledError:
            # SSE 连接被取消（客户端断开或服务器关闭），静默处理
            pass
        except Exception as e:
            # 先尝试告知客户端错误（如果还能 yield），再检查连接状态
            try:
                disconnected = await request.is_disconnected()
            except Exception:
                disconnected = False
            if not disconnected:
                yield {"event": "error", "data": json.dumps({"error": str(e)})}
        finally:
            # 确保 async generator 被正确关闭（释放 LLM 连接等资源）
            try:
                await run_gen.aclose()
            except Exception:
                pass
            # 保存历史记录到持久化存储（即使客户端已断开）
            try:
                runner._save_history()
            except Exception:
                pass

    return EventSourceResponse(event_stream())


@app.post("/api/chat/respond")
async def chat_respond(request: Request):
    """用户回复 Agent 的 ask_user / need_confirm 请求

    当 Agent 暂停等待用户输入时，前端调用此端点将用户回复发回，
    唤醒阻塞的 Agent 循环继续执行。
    """
    body = await request.json()
    session_id = body.get("session_id", "")
    response_data = body.get("response", {})

    if not session_id:
        raise HTTPException(status_code=400, detail="session_id is required")

    runner = session_runners.get(session_id)
    if runner:
        _touch_session(session_id)
        runner.respond_user(response_data)
        return JSONResponse({"status": "ok"})

    # 多 Agent 模式：唤醒指定任务中等待用户回复的 Agent
    if engine:
        engine.respond_user(session_id, response_data)
        return JSONResponse({"status": "ok"})

    raise HTTPException(status_code=404, detail="Session not found")


@app.post("/api/chat/cancel")
async def cancel_chat(request: Request):
    """终止指定会话当前正在运行的任务

    通知后端立即停止当前 Agent 循环（LLM 调用、工具执行等），
    并优雅地保存历史记录。CLI 在用户按 Ctrl+C 时调用此端点。
    """
    body = await request.json()
    session_id = body.get("session_id", "")

    if not session_id:
        raise HTTPException(status_code=400, detail="session_id is required")

    runner = session_runners.get(session_id)
    if runner:
        runner.cancel()
        return JSONResponse({"status": "cancelled", "session_id": session_id})

    # 多 Agent 模式：取消指定任务
    if engine:
        engine.cancel_task(session_id)
        return JSONResponse({"status": "cancelled", "session_id": session_id})

    raise HTTPException(status_code=404, detail="Session not found")


@app.post("/api/chat/multi-agent")
async def chat_multi_agent(request: Request):
    """多 Agent 协同对话 (SSE 流式)"""
    if not engine:
        raise HTTPException(status_code=503, detail="Engine not initialized")

    body = await request.json()
    message = body.get("message", "")
    session_id = body.get("session_id", "")

    if not message:
        raise HTTPException(status_code=400, detail="message is required")

    task_id = await engine.submit_task(message, session_id=session_id)
    event_queue = engine.get_event_queue(task_id)

    async def event_stream():
        try:
            yield {"event": "start", "data": json.dumps({
                "status": "started", "task_id": task_id, "mode": "multi-agent",
                "session_id": task_id,
            })}

            # 转发进度事件到 SSE
            while True:
                try:
                    event = await asyncio.wait_for(event_queue.get(), timeout=600.0)
                except asyncio.TimeoutError:
                    yield {"event": "timeout", "data": json.dumps({"error": "任务超时"})}
                    break

                # 根据 phase 分派 SSE 事件类型：终止事件用独立类型，其余作为 progress 转发
                phase = event.get("phase")
                if phase == "task_complete":
                    yield {"event": "task_complete", "data": json.dumps(event, ensure_ascii=False)}
                    break
                elif phase == "ask_user":
                    yield {"event": "ask_user", "data": json.dumps(event, ensure_ascii=False)}
                    # 不 break：Agent 正在等待用户回复，回复后会发布后续事件，SSE 继续转发
                elif phase == "need_confirm":
                    yield {"event": "need_confirm", "data": json.dumps(event, ensure_ascii=False)}
                    # 不 break：Agent 正在等待用户确认，确认后会发布后续事件，SSE 继续转发
                else:
                    yield {"event": "progress", "data": json.dumps(event, ensure_ascii=False)}

                # 检查客户端是否已断开
                try:
                    if await request.is_disconnected():
                        break
                except Exception:
                    pass

            yield {"event": "done", "data": json.dumps({"status": "completed"})}
        except asyncio.CancelledError:
            pass
        finally:
            # 客户端断开或任务完成时，清理事件订阅防止内存泄漏
            if engine:
                engine.bus.unsubscribe("progress", event_queue)

    return EventSourceResponse(event_stream())


# ===== 配置接口 =====

@app.get("/api/config")
async def get_config():
    """获取当前配置"""
    config = config_loader.load()
    return JSONResponse({
        "model": {
            "provider": config.model.provider,
            "name": config.model.name,
            "base_url": config.model.base_url,
            "max_tokens": config.model.max_tokens,
        },
        "profiles": config.profiles,
        "defaults": config.defaults,
    })


@app.get("/api/config/list")
async def list_profiles():
    """列出所有模型预设"""
    config = config_loader.load()
    profiles = config.profiles
    return JSONResponse({
        "default": config.defaults.get("profile", ""),
        "profiles": {name: {"name": p.get("name", ""), "base_url": p.get("base_url", "")}
                     for name, p in profiles.items()},
    })


@app.post("/api/tools/confirm")
async def confirm_tool(request: Request):
    """确认/拒绝工具执行"""
    body = await request.json()
    tool_call_id = body.get("tool_call_id", "")
    approved = body.get("approved", False)
    return JSONResponse({"tool_call_id": tool_call_id, "approved": approved})


# ===== Session 管理接口 =====

@app.get("/api/sessions")
async def list_sessions():
    """列出所有活跃会话"""
    now = time.monotonic()
    sessions = []
    for session_id, runner in session_runners.items():
        last_active = _session_last_active.get(session_id, 0)
        idle_seconds = int(now - last_active) if last_active else 0
        sessions.append({
            "session_id": session_id,
            "workspace": runner.workspace,
            "history_length": len(runner.history),
            "idle_seconds": idle_seconds,
            "ttl_seconds": SESSION_TTL_SECONDS,
        })
    return JSONResponse({"sessions": sessions})


@app.get("/api/sessions/history")
async def list_session_history(workspace: str = ""):
    """列出持久化的历史会话，可按 workspace 过滤"""
    from .agent.history import ConversationStore
    store = ConversationStore()
    all_sessions = store.list_all()

    if workspace:
        # 规范化路径用于比较
        ws_normalized = str(Path(workspace).resolve())
        all_sessions = [
            s for s in all_sessions
            if s.get("metadata", {}).get("workspace", "") == ws_normalized
        ]

    # 标记当前活跃的 session
    for s in all_sessions:
        s["is_active"] = s["id"] in session_runners
        s["active_workspace"] = (
            session_runners[s["id"]].workspace
            if s["id"] in session_runners else ""
        )

    return JSONResponse({"sessions": all_sessions})


@app.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str):
    """删除指定会话"""
    if session_id in session_runners:
        await _close_runner(session_id)
        return JSONResponse({"status": "deleted", "session_id": session_id})
    else:
        raise HTTPException(status_code=404, detail="Session not found")


@app.post("/api/sessions/{session_id}/clear")
async def clear_session(session_id: str):
    """清空指定会话的历史记录"""
    if session_id in session_runners:
        runner = session_runners[session_id]
        runner.history = []
        runner._save_history()
        return JSONResponse({"status": "cleared", "session_id": session_id})
    else:
        raise HTTPException(status_code=404, detail="Session not found")


@app.get("/api/sessions/{session_id}/messages")
async def get_session_messages(session_id: str):
    """获取指定会话的完整消息历史（从持久化存储加载）"""
    from .agent.history import ConversationStore
    store = ConversationStore()
    data = store.load(session_id)

    if not data:
        raise HTTPException(status_code=404, detail="Session not found in storage")

    return JSONResponse({
        "session_id": session_id,
        "title": data.get("title", ""),
        "created_at": data.get("created_at"),
        "updated_at": data.get("updated_at"),
        "messages": data.get("messages", []),
        "metadata": data.get("metadata", {}),
    })


# ===== 文件接口 =====

@app.get("/api/files")
async def list_files(path: str = "."):
    """列出目录文件"""
    p = Path(path)
    if not p.is_absolute():
        p = Path.cwd() / p
    if not p.exists():
        raise HTTPException(status_code=404, detail="目录不存在")

    files = []
    for entry in sorted(p.iterdir(), key=lambda e: (e.is_file(), e.name)):
        if entry.name.startswith("."):
            continue
        files.append({
            "name": entry.name,
            "type": "directory" if entry.is_dir() else "file",
            "size": entry.stat().st_size if entry.is_file() else 0,
        })
    return JSONResponse({"path": str(p), "files": files})


@app.get("/api/files/{file_path:path}")
async def read_file(file_path: str):
    """读取文件内容"""
    p = Path(file_path)
    if not p.is_absolute():
        p = Path.cwd() / p
    if not p.exists():
        raise HTTPException(status_code=404, detail="文件不存在")

    try:
        content = p.read_text(encoding="utf-8", errors="replace")
        return JSONResponse({"path": str(p), "content": content, "size": len(content)})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ===== 健康检查诊断 =====

@app.get("/api/doctor")
async def doctor():
    """系统诊断"""
    config = config_loader.load()
    llm_health = {"ok": False, "error": "Engine not running"}

    if engine:
        llm_health = await engine.llm.health_check()

    checks = {
        "config": {"ok": config_loader.GLOBAL_CONFIG_PATH.exists(),
                   "path": str(config_loader.GLOBAL_CONFIG_PATH)},
        "llm": llm_health,
        "engine": {"ok": engine is not None and engine._running},
        "workspace": str(Path.cwd()),
        "sessions": {
            "active": len(session_runners),
            "ttl_seconds": SESSION_TTL_SECONDS,
        },
    }

    all_ok = all(v.get("ok", False) if isinstance(v, dict) else v for v in checks.values())
    return JSONResponse({"status": "ok" if all_ok else "issues_found", "checks": checks})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8765)
