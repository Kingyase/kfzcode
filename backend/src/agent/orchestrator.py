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
                 review_timeout: int = 180):
        super().__init__(AgentRole.ORCHESTRATOR, bus, llm, registry)
        self.workspace = workspace
        self.max_iterations = max_iterations
        self.auto_confirm_on_pass = auto_confirm_on_pass
        self.review_timeout = review_timeout
        self.current_iteration = 0
        self.task_id = ""
        self.session_id = ""
        self.conversation_store = ConversationStore()

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
        task_desc = user_msg.payload.get("description", "")
        new_session_id = user_msg.payload.get("session_id", "")

        # 如果有session_id，加载历史并保留上下文
        if new_session_id:
            self.session_id = new_session_id
            self._load_history()
        else:
            self.reset_context()

        try:
            # Step 1: 分析任务
            await self.bus.publish_event("progress", {
                "phase": "analysis",
                "agent": "orchestrator",
                "content": f"正在分析任务: {task_desc}",
            })

            analysis = await self._analyze_task(task_desc)

            # Step 2: 构建编码任务
            coder_task = CoderTaskPayload(
                description=task_desc,
                constraints=analysis.get("constraints", []),
                project_conventions=analysis.get("conventions", ""),
            )

            # Step 3: 主循环
            while self.current_iteration < self.max_iterations:
                self.current_iteration += 1

                await self.bus.publish_event("progress", {
                    "phase": "iteration",
                    "iteration": self.current_iteration,
                    "max": self.max_iterations,
                })

                # 派发编码任务
                try:
                    coder_result = await self._dispatch_coder(coder_task)
                except AgentTimeoutError:
                    await self._notify_user_of_error("编码阶段超时，Coder 未能在限定时间内完成任务。")
                    return
                except Exception as e:
                    await self._notify_user_of_error(f"编码阶段异常: {e}")
                    return

                if coder_result.status == "failed":
                    await self._ask_user_for_direction(coder_result)
                    return

                # 派发验证任务
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
                    })
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
                    try:
                        await self._dispatch_fix(fix_payload)
                    except AgentTimeoutError:
                        await self._notify_user_of_error("修复阶段超时，Coder 未能在限定时间内完成修复。")
                        return
                    except Exception as e:
                        await self._notify_user_of_error(f"修复阶段异常: {e}")
                        return
                else:
                    await self._ask_user_on_exhausted(test_result)

        except Exception as e:
            await self._notify_user_of_error(f"调度循环意外异常: {e}")
        finally:
            self._reset()

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
        })

        msg = Message(
            type=MessageType.TASK_ASSIGN,
            from_agent=AgentRole.ORCHESTRATOR,
            to_agent=AgentRole.CODER,
            task_id=self.task_id,
            iteration=self.current_iteration,
            payload=task.to_dict(),
        )

        reply = await self.bus.request(msg, timeout=300.0)
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
        })

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

        reply = await self.bus.request(msg, timeout=self.review_timeout)
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

        reply = await self.bus.request(msg, timeout=300.0)
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

    async def _report_success(self, coder: CoderResultPayload, test: TestResultPayload) -> None:
        """任务成功完成"""
        changed = coder.changed_files
        created = coder.created_files
        summary_parts = ["✅ 任务完成！\n"]
        if created:
            summary_parts.append(f"新建文件: {', '.join(created)}")
        if changed:
            summary_parts.append(f"修改文件: {len(changed)} 个")
        summary_parts.append(f"\n{test.review_summary}")

        await self.bus.publish_event("task_complete", {
            "status": "success",
            "summary": "\n".join(summary_parts),
            "created_files": created,
            "changed_files": changed,
            "iterations": self.current_iteration,
        })

    async def _ask_user_for_direction(self, coder_result: CoderResultPayload) -> None:
        """Coder 失败，向用户求助"""
        await self.bus.publish_event("ask_user", {
            "question": f"编码任务未成功完成。\n{coder_result.notes}\n你希望如何处理？",
            "question_type": "single_choice",
            "header": "任务失败",
            "options": [
                {"label": "重试", "description": "让 Coder 重新尝试"},
                {"label": "简化需求", "description": "调整需求后重试"},
                {"label": "放弃", "description": "不再继续此任务"},
            ],
        })

    async def _ask_user_on_exhausted(self, test_result: TestResultPayload) -> None:
        """3轮后仍有问题，与用户交互确认"""
        issues_summary = "\n".join(
            f"  [{i.severity}] {i.title} ({i.file_path}"
            + (f":{i.line_number}" if i.line_number else "")
            + ")"
            for i in test_result.issues
        )

        await self.bus.publish_event("ask_user", {
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
        })

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
        try:
            data = self.conversation_store.load(self.session_id)
            if data and "messages" in data:
                self.conversation_history = [
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
            self.conversation_history = []

    def _save_history(self) -> None:
        """保存对话历史到持久化存储"""
        if not self.session_id or not self.conversation_history:
            return
        try:
            title = (self.conversation_history[0].content or "")[:50]
            self.conversation_store.save(
                conv_id=self.session_id,
                title=title,
                messages=self.conversation_history,
                metadata={"workspace": self.workspace},
            )
        except Exception:
            pass

    async def _notify_user_of_error(self, error_msg: str) -> None:
        """调度异常时通知用户"""
        await self.bus.publish_event("task_complete", {
            "status": "error",
            "summary": f"❌ 任务异常中断\n\n{error_msg}",
            "error": error_msg,
        })
