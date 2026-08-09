"""KFZCode 核心测试套件"""
import math
from utils import add
import sys
import os
import json
import tempfile
import asyncio
import pytest

# 添加 src 到路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


# ===== 工具函数测试 =====

class TestUtils:
    """测试工具函数"""

    def test_add_two_positive_numbers(self):
        assert add(1, 2) == 3

    def test_add_positive_and_negative(self):
        assert add(5, -3) == 2

    def test_add_two_negative_numbers(self):
        assert add(-4, -6) == -10

    def test_add_zero(self):
        assert add(0, 7) == 7
        assert add(7, 0) == 7
        assert add(0, 0) == 0

    def test_add_floats(self):
        assert add(1.5, 2.5) == 4.0

    def test_add_large_numbers(self):
        assert add(1_000_000, 2_000_000) == 3_000_000

    def test_add_with_nan(self):
        assert math.isnan(add(float("nan"), 1.0))

    def test_add_with_infinity(self):
        assert add(float("inf"), 1) == float("inf")
        assert add(float("-inf"), -1) == float("-inf")


# ===== 工具测试 =====

class TestTools:
    """测试工具系统"""

    @pytest.fixture
    def tmpdir(self):
        d = tempfile.mkdtemp()
        yield d
        import shutil
        try:
            shutil.rmtree(d, ignore_errors=True)
        except PermissionError:
            pass  # Windows 文件锁问题

    def test_tool_registry(self):
        from tools.base import ToolRegistry
        from tools.file_read import FileReadTool
        from tools.file_write import FileWriteTool
        from tools.shell import ShellTool

        registry = ToolRegistry()
        registry.register_many([
            FileReadTool("."), FileWriteTool("."), ShellTool(".")
        ])
        assert len(registry.get_definitions()) == 3
        assert registry.get("read_file") is not None
        assert registry.get("write_file") is not None
        assert registry.get("unknown_tool") is None

    @pytest.mark.asyncio
    async def test_file_read_write(self, tmpdir):
        from tools.file_read import FileReadTool
        from tools.file_write import FileWriteTool

        writer = FileWriteTool(tmpdir)
        reader = FileReadTool(tmpdir)

        # 写入
        result = await writer.execute("test.py", "print('hello')\n")
        assert result.success

        # 读取
        result = await reader.execute("test.py")
        assert result.success
        assert "hello" in result.output

    @pytest.mark.asyncio
    async def test_file_edit(self, tmpdir):
        from tools.file_write import FileWriteTool, FileEditTool
        from tools.file_read import FileReadTool

        await FileWriteTool(tmpdir).execute("test.py", "line1\nline2\nline3\n")

        editor = FileEditTool(tmpdir)
        result = await editor.execute("test.py", "line2", "modified_line2")
        assert result.success

        content = FileReadTool(tmpdir)
        result = await content.execute("test.py")
        assert "modified_line2" in result.output
        assert "line2" not in result.output.replace("modified_line2", "")

    @pytest.mark.asyncio
    async def test_shell(self, tmpdir):
        from tools.shell import ShellTool

        shell = ShellTool(tmpdir)
        result = await shell.execute('echo "hello"')
        assert result.success
        assert "hello" in result.output

    @pytest.mark.asyncio
    async def test_dangerous_command_blocked(self, tmpdir):
        from tools.shell import ShellTool

        shell = ShellTool(tmpdir)
        result = await shell.execute("rm -rf /")
        assert not result.success
        assert "拒绝" in result.error

    @pytest.mark.asyncio
    async def test_grep(self, tmpdir):
        from tools.file_write import FileWriteTool
        from tools.search import GrepTool

        await FileWriteTool(tmpdir).execute("a.py", "def hello():\n    return 'world'\n")
        await FileWriteTool(tmpdir).execute("b.py", "x = 42\n")

        grepper = GrepTool(tmpdir)
        result = await grepper.execute(pattern="hello", directory=".")
        assert result.success
        assert "a.py" in result.output

    @pytest.mark.asyncio
    async def test_glob(self, tmpdir):
        from tools.file_write import FileWriteTool
        from tools.search import GlobTool

        await FileWriteTool(tmpdir).execute("a.py", "")
        await FileWriteTool(tmpdir).execute("b.ts", "")
        await FileWriteTool(tmpdir).execute("c.py", "")

        glober = GlobTool(tmpdir)
        result = await glober.execute(pattern="*.py")
        assert result.success
        assert "a.py" in result.output
        assert "c.py" in result.output
        assert "b.ts" not in result.output


# ===== 配置测试 =====

class TestConfig:
    def test_default_config(self):
        from config import KFZCodeConfig

        config = KFZCodeConfig()
        assert config.model.name == "deepv4-large"
        assert config.model.max_tokens == 8192
        assert config.display.thinking == "collapsed"
        assert config.orchestrator.max_iterations == 3

    def test_custom_config(self):
        from config import KFZCodeConfig, ModelConfig, DisplayConfig

        config = KFZCodeConfig(
            model=ModelConfig(name="custom-model", base_url="http://custom:8000/v1"),
            display=DisplayConfig(thinking="expanded"),
        )
        assert config.model.name == "custom-model"
        assert config.display.thinking == "expanded"


# ===== Agent 消息协议测试 =====

class TestMessages:
    def test_coder_task_roundtrip(self):
        from agent.messages import CoderTaskPayload

        original = CoderTaskPayload(
            description="Create login API",
            constraints=["Use FastAPI", "Follow PEP8"],
            files_to_modify=["router.py"],
        )
        d = original.to_dict()
        restored = CoderTaskPayload.from_dict(d)
        assert restored.description == original.description
        assert restored.constraints == original.constraints
        assert restored.files_to_modify == original.files_to_modify

    def test_issue_roundtrip(self):
        from agent.messages import Issue

        original = Issue(
            severity="critical",
            category="test_failure",
            file_path="test_auth.py",
            line_number=42,
            title="Token expiry not handled",
            fix_suggestion="Add expiry check before returning 200",
        )
        d = original.to_dict()
        restored = Issue.from_dict(d)
        assert restored.severity == "critical"
        assert restored.title == original.title
        assert restored.line_number == 42

    def test_test_result_roundtrip(self):
        from agent.messages import TestResultPayload, Issue

        original = TestResultPayload(
            passed=False,
            test_output="1 failed, 2 passed",
            issues=[
                Issue(severity="critical", category="test_failure",
                      file_path="test.py", title="Test failed",
                      fix_suggestion="Fix it"),
                Issue(severity="warning", category="style",
                      file_path="app.py", title="Line too long",
                      fix_suggestion="Break into multiple lines"),
            ],
            review_summary="Found 2 issues",
        )
        d = original.to_dict()
        restored = TestResultPayload.from_dict(d)
        assert not restored.passed
        assert len(restored.issues) == 2
        assert restored.review_summary == "Found 2 issues"


# ===== 消息总线测试 =====

class TestMessageBus:
    @pytest.mark.asyncio
    async def test_send_receive(self):
        from agent.message_bus import MessageBus
        from agent.messages import AgentRole, Message, MessageType

        bus = MessageBus()
        msg = Message(
            type=MessageType.TASK_ASSIGN,
            from_agent=AgentRole.ORCHESTRATOR,
            to_agent=AgentRole.CODER,
            task_id="test-001",
            payload={"description": "Test"},
        )
        await bus.send(msg)
        received = await bus.receive(AgentRole.CODER, timeout=2.0)
        assert received.task_id == "test-001"
        assert received.payload["description"] == "Test"

    @pytest.mark.asyncio
    async def test_event_publish_subscribe(self):
        from agent.message_bus import MessageBus

        bus = MessageBus()
        queue = bus.subscribe("progress")
        await bus.publish_event("progress", {"status": "ok"})
        event = await asyncio.wait_for(queue.get(), timeout=1.0)
        assert event["status"] == "ok"


# ===== 上下文管理测试 =====

class TestContext:
    def test_compression(self):
        from agent.context import ContextManager
        from llm.types import LLMMessage

        cm = ContextManager(max_tokens=4000, reserve_for_response=1000)

        # 短消息不需要压缩
        short = [LLMMessage(role="user", content="Hello")]
        assert not cm.should_compress(short)

        # 长消息需要压缩
        long_text = "x" * 20000
        long_msgs = [
            LLMMessage(role="system", content="You are helpful"),
            LLMMessage(role="user", content=long_text),
        ]
        est = cm.estimate_tokens(long_msgs)
        assert est > cm.effective_limit

    def test_truncate(self):
        from agent.context import truncate_content

        assert truncate_content("hello", 100) == "hello"
        long = truncate_content("x" * 20000, 1000)
        assert len(long) < 20000


# ===== 类型测试 =====

class TestLLMTypes:
    def test_llm_response_to_message(self):
        from llm.types import LLMResponse, FunctionCall

        resp = LLMResponse(
            content="Here is the code",
            tool_calls=[
                FunctionCall(id="call_1", name="write_file",
                            arguments='{"file_path":"test.py","content":"x=1"}')
            ],
        )
        msg = resp.to_message()
        assert msg.role == "assistant"
        assert msg.content == "Here is the code"
        assert len(msg.tool_calls) == 1
        assert msg.tool_calls[0]["function"]["name"] == "write_file"

    def test_tool_result_to_message(self):
        from llm.types import ToolResult

        result = ToolResult(
            tool_call_id="call_1", name="write_file",
            success=True, output="File created"
        )
        msg = result.to_message()
        assert msg.role == "tool"
        assert msg.tool_call_id == "call_1"
        assert "File created" in msg.content

        # 测试截断
        long_output = "x" * 10000
        result2 = ToolResult(tool_call_id="c2", name="test", success=True, output=long_output)
        msg2 = result2.to_message()
        assert len(msg2.content) <= 8000
