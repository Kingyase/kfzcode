"""Orchestrator Agent — 主调度器，管理 Coder→Tester→修正 循环"""
from dataclasses import asdict

from .base_agent import BaseAgent
from .messages import (
    AgentRole, Message, MessageType,
    CoderTaskPayload, CoderResultPayload,
    TestTaskPayload, TestResultPayload,
    FixInstructionPayload, Issue,
)
from .message_bus import MessageBus, AgentTimeoutError
from .task_context import USER_RESPONSE_TIMEOUT
from .history import ConversationStore
from ..llm.client import DeepV4Client
from ..llm.types import LLMMessage
from ..tools.base import ToolRegistry


class OrchestratorAgent(BaseAgent):
    """主调度 Agent — 三线程协同的核心"""

    MAX_ITERATIONS = 3

    def __init__(self, bus: MessageBus, llm: DeepV4Client, registry: ToolRegistry,
                 workspace: str, max_iterations: int = 3,
                 auto_confirm_on_pass: bool = True,
                 review_timeout: int = 180,
                 rollback_callback=None):
        super().__init__(AgentRole.ORCHESTRATOR, bus, llm, registry)
        self.workspace = workspace
        self.max_iterations = max_iterations
        self.auto_confirm_on_pass = auto_confirm_on_pass
        self.review_timeout = review_timeout
        self.current_iteration = 0
        self.task_id = ""
        self.session_id = ""
        self.conversation_store = ConversationStore()
        # 回滚回调（由 Engine 注入），取消/异常时回滚本任务的文件修改
        self.rollback_callback = rollback_callback
        # 当前任务结局: success / partial / cancelled / error
        self._task_outcome = ""

    @property
    def system_prompt(self) -> str:
        return f"""你是 KFZCode 的调度 Agent (Orchestrator)。你的职责是：
1. 分析用户需求，拆解为可执行的编码任务
2. 将任务派发给 Coder Agent
3. Coder 完成后，派发给 Tester Agent 验证
4. 根据 Tester 反馈决定: 通过 / 修复 / 与用户交互
5. 最多 {self.max_iterations} 轮自动修复，超过后询问用户

当前工作目录: {self.workspace}

可用工具: ask_user（与用户交互）"""

    async def handle_message(self, msg: Message) -> None:
        if msg.type == MessageType.TASK_ASSIGN:
            await self._run_orchestration_loop(msg)

    async def _run_orchestration_loop(self, user_msg: Message) -> None:
        """核心调度循环"""
        self.task_id = user_msg.task_id
        self._task_outcome = ""  # 重置结局
        task_desc = user_msg.payload.get("description", "")
        new_session_id = user_msg.payload.get("session_id", "")

        # 如果有session_id，加载历史并保留上下文
        if new_session_id:
            self.session_id = new_session_id
            self._load_history()
        else:
            self.reset_context(self.task_id)

        try:
            # Step 0: 判断任务是否需要编码 —— 普通对话/问答直接回复，不走 Coder/Tester 流程
            needs_coding = await self._classify_task(task_desc)
            if not needs_coding:
                await self.bus.publish_event("progress", {
                    "phase": "analysis",
                    "agent": "orchestrator",
                    "content": f"正在回复: {task_desc}",
                }, task_id=self.task_id)
                reply = await self._chat_reply(task_desc)
                await self.bus.publish_event("progress", {
                    "phase": "task_complete",
                    "status": "success",
                    "summary": reply,
                    "is_chat": True,  # 标记为普通对话回复，CLI 以正常消息展示
                }, task_id=self.task_id)
                return

            # Step 1: 分析任务
            await self.bus.publish_event("progress", {
                "phase": "analysis",
                "agent": "orchestrator",
                "content": f"正在分析任务: {task_desc}",
            }, task_id=self.task_id)

            analysis = await self._analyze_task(task_desc)

            # Step 2: 构建编码任务
            coder_task = CoderTaskPayload(
                description=task_desc,
                constraints=analysis.get("constraints", []),
                project_conventions=analysis.get("conventions", ""),
            )

            # Step 3: 主循环
            while self.current_iteration < self.max_iterations:
                if await self._check_cancel():
                    return
                self.current_iteration += 1

                await self.bus.publish_event("progress", {
                    "phase": "iteration",
                    "iteration": self.current_iteration,
                    "max": self.max_iterations,
                }, task_id=self.task_id)

                # 派发编码任务（失败后支持重试 / 简化需求 / 放弃）
                while True:
                    if await self._check_cancel():
                        return
                    try:
                        coder_result = await self._dispatch_coder(coder_task)
                    except AgentTimeoutError:
                        await self._notify_user_of_error("编码阶段超时，Coder 未能在限定时间内完成任务。")
                        return
                    except Exception as e:
                        await self._notify_user_of_error(f"编码阶段异常: {e}")
                        return

                    if coder_result.status != "failed":
                        break  # 编码成功（或部分成功），继续验证

                    # Coder 失败，向用户求助并等待回复
                    decision = await self._ask_user_for_direction(coder_result)
                    if decision == "重试":
                        continue  # 用原任务重新派发 Coder
                    elif decision == "简化需求":
                        new_desc = await self._ask_user_for_text("请输入简化后的需求描述：")
                        if not new_desc:
                            await self._report_cancelled("用户取消了任务")
                            return
                        task_desc = new_desc
                        analysis = await self._analyze_task(task_desc)
                        coder_task = CoderTaskPayload(
                            description=task_desc,
                            constraints=analysis.get("constraints", []),
                            project_conventions=analysis.get("conventions", ""),
                        )
                        continue  # 用新需求重新派发 Coder
                    else:  # 放弃或取消
                        ctx = self._task_context()
                        if ctx and ctx.is_cancelled():
                            await self._report_cancelled("任务已被用户取消")
                        else:
                            await self._report_cancelled("用户选择放弃任务")
                        return

                # 派发验证任务
                if await self._check_cancel():
                    return
                try:
                    test_result = await self._dispatch_tester(coder_result)
                except AgentTimeoutError:
                    await self._notify_user_of_error("审查阶段超时，Tester 未能在限定时间内完成审查。")
                    return
                except Exception as e:
                    await self._notify_user_of_error(f"审查阶段异常: {e}")
                    return

                if test_result.passed:
                    await self._report_success(coder_result, test_result)
                    return

                # 有问题，准备下一轮
                if self.current_iteration < self.max_iterations:
                    await self.bus.publish_event("progress", {
                        "phase": "fixing",
                        "iteration": self.current_iteration + 1,
                        "issues": [i.title for i in test_result.issues],
                    }, task_id=self.task_id)
                    coder_task = CoderTaskPayload(
                        description=task_desc,
                        constraints=analysis.get("constraints", []),
                        project_conventions=analysis.get("conventions", ""),
                    )
                    # 构建修复指令
                    fix_payload = FixInstructionPayload(
                        original_task=coder_task,
                        issues_to_fix=test_result.issues,
                        additional_context=f"上一轮实现后 Tester 发现了 {len(test_result.issues)} 个问题，请修复。",
                    )
                    if await self._check_cancel():
                        return
                    try:
                        await self._dispatch_fix(fix_payload)
                    except AgentTimeoutError:
                        await self._notify_user_of_error("修复阶段超时，Coder 未能在限定时间内完成修复。")
                        return
                    except Exception as e:
                        await self._notify_user_of_error(f"修复阶段异常: {e}")
                        return
                else:
                    # 达到最大迭代次数仍有问题，向用户求助并等待回复
                    decision = await self._ask_user_on_exhausted(test_result)
                    if decision == "继续修复":
                        fix_payload = FixInstructionPayload(
                            original_task=coder_task,
                            issues_to_fix=test_result.issues,
                            additional_context="用户要求继续修复。",
                        )
                        try:
                            fixed = await self._dispatch_fix(fix_payload)
                        except AgentTimeoutError:
                            await self._notify_user_of_error("修复阶段超时，Coder 未能在限定时间内完成修复。")
                            return
                        except Exception as e:
                            await self._notify_user_of_error(f"修复阶段异常: {e}")
                            return
                        # 修复后再验证一次
                        try:
                            retest = await self._dispatch_tester(fixed)
                        except AgentTimeoutError:
                            await self._notify_user_of_error("审查阶段超时，Tester 未能在限定时间内完成审查。")
                            return
                        except Exception as e:
                            await self._notify_user_of_error(f"审查阶段异常: {e}")
                            return
                        if retest.passed:
                            await self._report_success(fixed, retest, note="（用户要求继续修复后通过）")
                        else:
                            await self._report_partial(fixed, retest, "用户要求继续修复后仍有问题")
                        return
                    elif decision == "验收当前代码":
                        await self._report_success(coder_result, test_result, note="（用户已验收当前代码）")
                        return
                    else:  # 跳过这些问题 或 取消
                        ctx = self._task_context()
                        if ctx and ctx.is_cancelled():
                            await self._report_cancelled("任务已被用户取消")
                        else:
                            await self._report_partial(coder_result, test_result, "问题已按用户要求标记为已知问题")
                        return

        except Exception as e:
            await self._notify_user_of_error(f"调度循环意外异常: {e}")
        finally:
            self._reset()
            # 取消/异常时回滚本任务的文件修改
            if self._task_outcome in ("cancelled", "error"):
                await self._rollback_task(user_msg.task_id)
            # 通知 Coder/Tester 清理该任务的历史，避免内存泄漏
            for target in (AgentRole.CODER, AgentRole.TESTER):
                await self.bus.send(Message(
                    type=MessageType.CANCEL,
                    from_agent=AgentRole.ORCHESTRATOR,
                    to_agent=target,
                    task_id=user_msg.task_id,
                    payload={"reason": "task_finished"},
                ))
            # 兜底清理 TaskContext（释放备份内存 + 移除任务）
            self.bus.remove_task(user_msg.task_id)

    async def _classify_task(self, task_desc: str) -> bool:
        """判断用户消息是否需要执行编码任务。

        Returns:
            True 表示需要 Coder/Tester 执行编码流程；
            False 表示普通对话/问答，应直接回复。
        """
        prompt = """你是任务分类器。判断用户消息是否需要执行编码任务。

需要编码任务的情况（回复 YES）：
- 编写、修改、创建代码或文件
- 调试、修复 bug、重构代码
- 运行测试、构建、部署等涉及项目文件的操作

不需要编码任务的情况（回复 NO）：
- 简单问候、闲聊（如"你好"）
- 纯知识问答、概念解释、技术咨询
- 不涉及修改任何代码或文件的请求

只回复一个单词：YES 或 NO。"""
        messages = [
            LLMMessage(role="system", content=prompt),
            LLMMessage(role="user", content=task_desc),
        ]
        try:
            response = await self.llm.chat(messages, stream=True)
            return "YES" in (response.content or "").upper()
        except Exception:
            # 分类失败时默认走编码流程，避免因分类异常中断任务
            return True

    async def _chat_reply(self, message: str) -> str:
        """普通对话，直接用 LLM 简洁回复（不进入编码流程）。"""
        prompt = "你是 KFZCode 内网 AI 编程助手。请简洁、友好地回复用户。"
        messages = [
            LLMMessage(role="system", content=prompt),
            LLMMessage(role="user", content=message),
        ]
        response = await self.llm.chat(messages, stream=True)
        return response.content or ""

    async def _analyze_task(self, task_desc: str) -> dict:
        """让 LLM 分析任务，并解析结构化约束"""
        prompt = f"""分析以下编码任务，输出:
1. 需要修改/创建的文件列表
2. 约束条件（语言、框架、规范等）
3. 项目约定（如果有的话）

任务: {task_desc}

请简要输出分析结果。"""
        response = await self.call_llm(prompt)
        constraints = self._extract_constraints(response.content)
        return {
            "analysis": response.content,
            "constraints": constraints,
            "conventions": response.content,  # 将分析全文传给 Coder 作为参考
        }

    def _extract_constraints(self, analysis: str) -> list[str]:
        """从 LLM 分析中提取约束条件"""
        constraints: list[str] = []
        if not analysis:
            return constraints
        # 在"约束"相关段落中搜索列表项
        in_constraint_section = False
        for line in analysis.split("\n"):
            line = line.strip()
            if not line:
                continue
            lower = line.lower()
            if any(kw in lower for kw in ["约束", "constraint", "规范", "convention", "2.", "2)", "第二步"]):
                in_constraint_section = True
                continue
            if any(kw in lower for kw in ["项目约定", "3.", "3)", "第三步"]):
                in_constraint_section = False
                continue
            if in_constraint_section and (line.startswith("-") or line.startswith("*") or line.startswith("•")
                                          or (len(line) > 1 and line[0].isdigit())):
                clean = line.lstrip("-*• 0123456789.)").strip()
                if clean and len(clean) > 3:
                    constraints.append(clean)
        return constraints[:10]  # 最多保留 10 条

    async def _dispatch_coder(self, task: CoderTaskPayload) -> CoderResultPayload:
        """派发编码任务给 Coder，等待结果"""
        await self.bus.publish_event("progress", {
            "agent": "coder", "status": "dispatched",
            "task": task.description, "iteration": self.current_iteration,
        }, task_id=self.task_id)

        msg = Message(
            type=MessageType.TASK_ASSIGN,
            from_agent=AgentRole.ORCHESTRATOR,
            to_agent=AgentRole.CODER,
            task_id=self.task_id,
            iteration=self.current_iteration,
            payload=task.to_dict(),
        )

        reply = await self.bus.request(msg, timeout=USER_RESPONSE_TIMEOUT + 300)
        try:
            return CoderResultPayload.from_dict(reply.payload)
        except Exception:
            return CoderResultPayload(
                status="failed",
                notes=f"Coder 返回数据格式异常: {str(reply.payload)[:500]}",
            )

    async def _dispatch_tester(self, coder_result: CoderResultPayload) -> TestResultPayload:
        """派发验证任务给 Tester，等待结果"""
        await self.bus.publish_event("progress", {
            "agent": "tester", "status": "dispatched",
            "iteration": self.current_iteration,
        }, task_id=self.task_id)

        task = TestTaskPayload(
            coder_result=coder_result,
            test_commands=self._infer_test_commands(coder_result),
            review_dimensions=["correctness", "security", "style", "performance"],
            changed_files_content={},
        )

        msg = Message(
            type=MessageType.TASK_ASSIGN,
            from_agent=AgentRole.ORCHESTRATOR,
            to_agent=AgentRole.TESTER,
            task_id=self.task_id,
            iteration=self.current_iteration,
            payload=task.to_dict(),
        )

        reply = await self.bus.request(msg, timeout=max(self.review_timeout, USER_RESPONSE_TIMEOUT + 300))
        try:
            return TestResultPayload.from_dict(reply.payload)
        except Exception:
            return TestResultPayload(
                passed=False,
                test_output="审查结果解析失败",
                issues=[Issue(
                    severity="warning",
                    category="style",
                    file_path="",
                    title="Tester 返回数据格式异常",
                    description=f"原始数据: {str(reply.payload)[:1000]}",
                    fix_suggestion="请人工检查代码变更。",
                )],
            )

    async def _dispatch_fix(self, fix_payload: FixInstructionPayload) -> CoderResultPayload:
        """派发修复指令给 Coder"""
        msg = Message(
            type=MessageType.FIX_INSTRUCTION,
            from_agent=AgentRole.ORCHESTRATOR,
            to_agent=AgentRole.CODER,
            task_id=self.task_id,
            iteration=self.current_iteration,
            payload=fix_payload.to_dict(),
        )

        reply = await self.bus.request(msg, timeout=USER_RESPONSE_TIMEOUT + 300)
        try:
            return CoderResultPayload.from_dict(reply.payload)
        except Exception:
            return CoderResultPayload(
                status="failed",
                notes=f"修复阶段 Coder 返回数据格式异常: {str(reply.payload)[:500]}",
            )

    def _infer_test_commands(self, coder_result: CoderResultPayload) -> list[str]:
        """从 Coder 结果推断测试命令"""
        commands = []
        for f in coder_result.created_files + [cf.get("path", "") for cf in coder_result.changed_files]:
            if "test_" in f or f.endswith("_test.py") or f.endswith(".test.ts"):
                commands.append(f"pytest {f} -v 2>&1 || true")
        if not commands:
            commands.append("pytest -x --tb=short 2>&1 || true")
            commands.append("ruff check . 2>&1 || true")
        return commands

    async def _report_success(self, coder: CoderResultPayload, test: TestResultPayload,
                              note: str = "") -> None:
        """任务成功完成"""
        self._task_outcome = "success"
        changed = coder.changed_files
        created = coder.created_files
        summary_parts = ["✅ 任务完成！\n"]
        if created:
            summary_parts.append(f"新建文件: {', '.join(created)}")
        if changed:
            summary_parts.append(f"修改文件: {len(changed)} 个")
        if note:
            summary_parts.append(note)
        summary_parts.append(f"\n{test.review_summary}")

        # 发布到 progress 队列（带 phase）以便 SSE 桥接能收到终止信号并结束流
        await self.bus.publish_event("progress", {
            "phase": "task_complete",
            "status": "success",
            "summary": "\n".join(summary_parts),
            "created_files": created,
            "changed_files": changed,
            "iterations": self.current_iteration,
        }, task_id=self.task_id)

    async def _report_partial(self, coder: CoderResultPayload, test: TestResultPayload | None,
                              note: str) -> None:
        """任务部分完成 / 问题被用户接受"""
        self._task_outcome = "partial"
        summary_parts = ["⚠️ 任务部分完成\n"]
        if coder.created_files:
            summary_parts.append(f"新建文件: {', '.join(coder.created_files)}")
        if coder.changed_files:
            summary_parts.append(f"修改文件: {len(coder.changed_files)} 个")
        summary_parts.append(f"\n{note}")
        if test:
            summary_parts.append(f"\n{test.review_summary}")

        await self.bus.publish_event("progress", {
            "phase": "task_complete",
            "status": "partial",
            "summary": "\n".join(summary_parts),
            "created_files": coder.created_files,
            "changed_files": coder.changed_files,
        }, task_id=self.task_id)

    async def _report_cancelled(self, reason: str) -> None:
        """任务被用户取消"""
        self._task_outcome = "cancelled"
        await self.bus.publish_event("progress", {
            "phase": "task_complete",
            "status": "cancelled",
            "summary": f"⏹ 任务已取消\n\n{reason}",
        }, task_id=self.task_id)

    async def _check_cancel(self) -> bool:
        """检查取消标志；若已取消则发布 cancelled 事件并返回 True。

        供调度循环在关键检查点调用，实现协作式中途取消。
        """
        ctx = self._task_context()
        if ctx and ctx.is_cancelled():
            await self._report_cancelled("任务已被用户取消")
            return True
        return False

    async def _ask_user_for_direction(self, coder_result: CoderResultPayload) -> str:
        """Coder 失败，向用户求助并等待回复。返回用户选择的方向标签。"""
        await self.bus.publish_event("progress", {
            "phase": "ask_user",
            "question": f"编码任务未成功完成。\n{coder_result.notes}\n你希望如何处理？",
            "question_type": "single_choice",
            "header": "任务失败",
            "options": [
                {"label": "重试", "description": "让 Coder 重新尝试"},
                {"label": "简化需求", "description": "调整需求后重试"},
                {"label": "放弃", "description": "不再继续此任务"},
            ],
        }, task_id=self.task_id)
        ctx = self._task_context()
        reply = await ctx.wait_for_user_response(timeout=USER_RESPONSE_TIMEOUT) if ctx else {"status": "timeout"}
        return reply.get("answer", "")

    async def _ask_user_for_text(self, question: str) -> str:
        """向用户提问并等待文本回复。返回用户输入内容（空串表示取消/超时）。"""
        await self.bus.publish_event("progress", {
            "phase": "ask_user",
            "question": question,
            "question_type": "text",
            "header": "需要输入",
            "options": [],
        }, task_id=self.task_id)
        ctx = self._task_context()
        reply = await ctx.wait_for_user_response(timeout=USER_RESPONSE_TIMEOUT) if ctx else {"status": "timeout"}
        if reply.get("status") == "timeout":
            return ""
        return reply.get("answer", "")

    async def _ask_user_on_exhausted(self, test_result: TestResultPayload) -> str:
        """3轮后仍有问题，与用户交互确认并等待回复。返回用户选择的方向标签。"""
        issues_summary = "\n".join(
            f"  [{i.severity}] {i.title} ({i.file_path}"
            + (f":{i.line_number}" if i.line_number else "")
            + ")"
            for i in test_result.issues
        )

        await self.bus.publish_event("progress", {
            "phase": "ask_user",
            "question": (
                f"已修复 {self.max_iterations} 轮，仍有 {len(test_result.issues)} 个问题:\n\n"
                f"{issues_summary}\n\n你希望如何处理？"
            ),
            "question_type": "single_choice",
            "header": "修复未完成",
            "options": [
                {"label": "继续修复", "description": "调整修复策略再试一轮"},
                {"label": "验收当前代码", "description": "接受现有代码，不再修改"},
                {"label": "跳过这些问题", "description": "临时标记为已知问题"},
            ],
        }, task_id=self.task_id)
        ctx = self._task_context()
        reply = await ctx.wait_for_user_response(timeout=USER_RESPONSE_TIMEOUT) if ctx else {"status": "timeout"}
        return reply.get("answer", "")

    def _reset(self) -> None:
        self._save_history()
        self.current_iteration = 0
        self.task_id = ""
        self.session_id = ""
        self.reset_context()

    def _load_history(self) -> None:
        """从持久化存储加载对话历史"""
        if not self.session_id:
            return
        history = self._history()
        try:
            data = self.conversation_store.load(self.session_id)
            if data and "messages" in data:
                history[:] = [
                    LLMMessage(
                        role=msg.get("role", ""),
                        content=msg.get("content") or "",
                        tool_calls=msg.get("tool_calls"),
                        tool_call_id=msg.get("tool_call_id"),
                        name=msg.get("name"),
                    )
                    for msg in data["messages"]
                ]
        except Exception:
            history.clear()

    def _save_history(self) -> None:
        """保存对话历史到持久化存储"""
        history = self._history()
        if not self.session_id or not history:
            return
        try:
            title = (history[0].content or "")[:50]
            self.conversation_store.save(
                conv_id=self.session_id,
                title=title,
                messages=history,
                metadata={"workspace": self.workspace},
            )
        except Exception:
            pass

    async def _notify_user_of_error(self, error_msg: str) -> None:
        """调度异常时通知用户"""
        self._task_outcome = "error"
        # 发布到 progress 队列（带 phase）以便 SSE 桥接能收到并结束流
        await self.bus.publish_event("progress", {
            "phase": "task_complete",
            "status": "error",
            "summary": f"❌ 任务异常中断\n\n{error_msg}",
            "error": error_msg,
        }, task_id=self.task_id)

    async def _rollback_task(self, task_id: str) -> None:
        """触发引擎回滚本任务的文件修改（若注入了回滚回调）。"""
        if self.rollback_callback:
            try:
                await self.rollback_callback(task_id)
            except Exception:
                pass
