"""Reviewer Agent — 测试审查"""
import json
from dataclasses import asdict

from .base_agent import BaseAgent
from .messages import (
    AgentRole, Message, MessageType,
    TestTaskPayload, TestResultPayload, Issue,
)
from .message_bus import MessageBus
from ..llm.client import DeepV4Client
from ..llm.types import FunctionCall
from ..tools.base import ToolRegistry


class ReviewerAgent(BaseAgent):
    """测试审查 Agent"""

    def __init__(self, bus: MessageBus, llm: DeepV4Client, registry: ToolRegistry, workspace: str):
        super().__init__(AgentRole.TESTER, bus, llm, registry)
        self.workspace = workspace

    @property
    def system_prompt(self) -> str:
        return f"""你是 KFZCode 的测试审查 Agent (Tester)。你的职责是验证 Coder 的代码质量。

## 工作原则
1. **运行测试**: 执行 Coder 编写的测试，收集所有失败信息
2. **代码审查**: 检查代码的正确性、安全性、性能、风格
3. **结构化输出**: 为每个发现的问题生成结构化 Issue，包含修复建议
4. **不做修改**: 你只输出审查报告，不修改任何代码
5. **全面但不挑剔**: 关注实质性问题，不要纠结于无关紧要的风格偏好

## 审查维度
- correctness: 逻辑正确性，边界条件处理
- security: 注入漏洞、敏感信息泄露、权限问题
- style: 代码风格、命名规范、注释充分性
- performance: 明显的性能问题（N+1查询等）

## 输出格式
对于每个发现的问题，请使用以下结构化格式：
```json
{{"issues": [
  {{
    "severity": "critical|warning|suggestion",
    "category": "test_failure|logic|security|style|performance",
    "file_path": "...",
    "line_number": null,
    "title": "简短描述",
    "description": "详细描述",
    "expected_behavior": "期望行为",
    "actual_behavior": "实际行为",
    "fix_suggestion": "给 Coder 的修复建议"
  }}
]}}
```

## 当前工作目录
{self.workspace}

## 可用工具
- execute_command: 运行测试命令
- read_file: 审查代码
- grep: 搜索特定模式
"""

    async def handle_message(self, msg: Message) -> None:
        if msg.type == MessageType.TASK_ASSIGN:
            await self._handle_review_task(msg)
        elif msg.type == MessageType.CANCEL:
            # 任务结束/取消：清理该任务的历史，避免内存泄漏
            self.reset_context(msg.task_id)

    async def _handle_review_task(self, msg: Message) -> None:
        """处理审查任务"""
        payload = TestTaskPayload.from_dict(msg.payload)
        self.reset_context(msg.task_id)

        await self.bus.publish_event("progress", {
            "agent": "tester",
            "status": "reviewing",
            "iteration": msg.iteration,
        }, task_id=msg.task_id)

        all_issues: list[Issue] = []

        # Step 1: 执行测试命令
        if payload.test_commands:
            prompt = self._build_test_prompt(payload)
            tools = self.registry.get_definitions()
            response = await self.call_llm(prompt, tools=tools)
            if response.tool_calls:
                response = await self.execute_tool_loop(response, tools)

            # 从对话历史收集测试执行结果
            test_output = self._collect_test_output()
        else:
            test_output = "无测试命令"

        # Step 2: LLM 代码审查
        review_prompt = self._build_review_prompt(payload, test_output)
        review_response = await self.call_llm(review_prompt)

        # 解析结构化 Issue
        review_issues = self._parse_issues(review_response.content)
        all_issues.extend(review_issues)

        # Step 3: 判定是否通过
        critical_count = sum(1 for i in all_issues if i.severity == "critical")
        warning_count = sum(1 for i in all_issues if i.severity == "warning")

        result = TestResultPayload(
            passed=(critical_count == 0),
            test_output=test_output,
            issues=all_issues,
            review_summary=self._build_summary(all_issues, critical_count, warning_count),
        )

        await self.bus.publish_event("progress", {
            "agent": "tester",
            "status": "done",
            "passed": result.passed,
            "issue_count": len(all_issues),
        }, task_id=msg.task_id)

        await self.send_result(msg, result.to_dict())

    def _build_test_prompt(self, payload: TestTaskPayload) -> str:
        parts = ["## 测试任务\n\n执行以下测试命令并报告结果:\n"]
        for cmd in payload.test_commands:
            parts.append(f"- `{cmd}`")
        parts.append("\n请逐个执行这些命令。")
        return "\n".join(parts)

    def _build_review_prompt(self, payload: TestTaskPayload, test_output: str) -> str:
        parts = ["## 代码审查任务\n"]
        parts.append(f"### 测试结果\n```\n{test_output[:3000]}\n```\n")
        parts.append("### 变更的文件\n")

        for path, content in payload.changed_files_content.items():
            if len(content) > 4000:
                content = content[:4000] + "\n... [截断]"
            parts.append(f"#### {path}\n```\n{content}\n```\n")

        parts.append("\n请对以上变更进行全面审查，输出结构化 Issue 列表(JSON格式)。")
        return "\n".join(parts)

    def _parse_issues(self, content: str) -> list[Issue]:
        """从 LLM 响应中解析结构化 Issue

        解析失败时不会静默丢弃 — 会创建一个 warning 级别的 Issue，
        并将原始审查内容作为描述，确保审查信息不丢失。"""
        issues = []
        try:
            # 尝试提取 JSON 块
            if "```json" in content:
                start = content.index("```json") + 7
                end = content.index("```", start)
                json_str = content[start:end].strip()
            elif "```" in content:
                start = content.index("```") + 3
                end = content.index("```", start)
                json_str = content[start:end].strip()
            elif "{" in content:
                start = content.index("{")
                end = content.rindex("}") + 1
                json_str = content[start:end]
            else:
                return issues

            data = json.loads(json_str)
            for item in data.get("issues", []):
                issues.append(Issue(
                    severity=item.get("severity", "warning"),
                    category=item.get("category", "style"),
                    file_path=item.get("file_path", ""),
                    line_number=item.get("line_number"),
                    title=item.get("title", ""),
                    description=item.get("description", ""),
                    expected_behavior=item.get("expected_behavior", ""),
                    actual_behavior=item.get("actual_behavior", ""),
                    fix_suggestion=item.get("fix_suggestion", ""),
                ))
        except (json.JSONDecodeError, ValueError, IndexError) as e:
            # 解析失败时不静默丢弃，生成一个包含原始响应的 warning issue
            issues.append(Issue(
                severity="warning",
                category="style",
                file_path="",
                title=f"代码审查结果解析失败（{type(e).__name__}），以下是原始审查内容",
                description=content[:2000],
                fix_suggestion="请人工检查审查结果，并确认代码变更是否正确。",
            ))
        return issues

    def _collect_test_output(self) -> str:
        """从对话历史收集测试输出"""
        outputs = []
        for msg in self._history():
            if msg.role == "tool" and msg.content:
                if any(cmd in msg.content for cmd in ["$ ", "pytest", "npm test", "go test"]):
                    outputs.append(msg.content[:1000])
        return "\n---\n".join(outputs) if outputs else ""

    def _build_summary(self, issues: list[Issue], critical: int, warning: int) -> str:
        if not issues:
            return "✅ 代码审查通过，未发现问题。"
        parts = [f"代码审查完成，发现 {len(issues)} 个问题:"]
        parts.append(f"  - 严重: {critical} 个")
        parts.append(f"  - 警告: {warning} 个")
        parts.append(f"  - 建议: {len(issues) - critical - warning} 个")
        return "\n".join(parts)
