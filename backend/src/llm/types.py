"""LLM 请求/响应核心类型定义"""
import base64
import mimetypes
from pathlib import Path
from typing import Any, Literal
from dataclasses import dataclass, field

# ===== 多模态 / 图片支持 =====

SUPPORTED_IMAGE_MIMES: set[str] = {
    "image/jpeg", "image/png", "image/gif", "image/webp",
    "image/bmp", "image/tiff", "image/svg+xml",
}
"""支持的图片 MIME 类型白名单"""

MAX_IMAGE_SIZE_BYTES = 20 * 1024 * 1024  # 20MB


def build_multimodal_content(text: str, images: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """文本 + 多张图片 → OpenAI 多模态 content 数组

    Args:
        text: 用户文本消息（可为空字符串）
        images: image_url content block 列表，由 image_to_content_block 生成
    Returns:
        OpenAI 多模态 content 数组，text 块在前、image 块在後
    """
    blocks: list[dict[str, Any]] = []
    if text:
        blocks.append({"type": "text", "text": text})
    blocks.extend(images)
    return blocks


def image_to_content_block(file_path: str, mime_type: str = "",
                           detail: str = "auto") -> dict[str, Any]:
    """本地图片文件 → OpenAI image_url content block

    读取磁盘上的图片，base64 编码后打包为 OpenAI 多模态格式。

    Args:
        file_path: 图片文件的绝对路径
        mime_type: MIME 类型（留空则自动推断）
        detail: OpenAI 图片细节参数，auto / low / high
    Returns:
        {"type": "image_url", "image_url": {"url": "data:...", "detail": "auto"}}
    Raises:
        FileNotFoundError: 文件不存在
        ValueError: 格式不支持或文件过大
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"图片文件不存在: {file_path}")

    file_size = path.stat().st_size
    if file_size > MAX_IMAGE_SIZE_BYTES:
        raise ValueError(
            f"图片文件过大 ({file_size / 1024 / 1024:.1f}MB > "
            f"{MAX_IMAGE_SIZE_BYTES // 1024 // 1024}MB): {file_path}"
        )

    if not mime_type:
        mime_type, _ = mimetypes.guess_type(file_path)
    if not mime_type:
        raise ValueError(f"无法识别图片格式: {file_path}")
    if mime_type not in SUPPORTED_IMAGE_MIMES:
        raise ValueError(
            f"不支持的图片格式 ({mime_type})，支持的类型: "
            f"{', '.join(sorted(SUPPORTED_IMAGE_MIMES))}"
        )

    data = path.read_bytes()
    b64 = base64.b64encode(data).decode("ascii")
    url = f"data:{mime_type};base64,{b64}"
    return {"type": "image_url", "image_url": {"url": url, "detail": detail}}


def extract_text_from_content(content: str | list[dict[str, Any]] | None) -> str:
    """从 content 提取纯文本（用于摘要、日志、token 估算等场景）

    - str → 原样返回
    - list → 拼接所有 text 块的文本
    - None → 返回空字符串
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    # list[dict] — OpenAI 多模态 content 数组
    return "\n".join(
        b.get("text", "") for b in content
        if isinstance(b, dict) and b.get("type") == "text"
    )


def count_images_in_content(content: str | list[dict[str, Any]] | None) -> int:
    """统计 content 中的图片数量（用于日志记录和大小估算）"""
    if content is None or isinstance(content, str):
        return 0
    return sum(1 for b in content if isinstance(b, dict) and b.get("type") == "image_url")


def strip_images_from_content(content: str | list[dict[str, Any]] | None) -> str | list[dict[str, Any]] | None:
    """存储时将 image_url 替换为占位符，避免 base64 撑爆历史数据库

    纯文本 content 原样返回；多模态数组内容会将 image 块替换为简短标记。
    """
    if content is None or isinstance(content, str):
        return content
    count = count_images_in_content(content)
    if count == 0:
        return content
    # 保留 text 块，丢弃 image 块，追加占位符文本
    parts = [b for b in content if isinstance(b, dict) and b.get("type") == "text"]
    parts.append({"type": "text", "text": f"[{count} 张图片(未保存)]"})
    return parts


@dataclass
class ToolDefinition:
    """传给 LLM 的工具定义 (OpenAI function calling 格式)"""
    name: str
    description: str
    parameters: dict[str, Any]


@dataclass
class FunctionCall:
    """LLM 返回的工具调用"""
    id: str
    name: str
    arguments: str  # JSON string


@dataclass
class ToolCallDelta:
    """流式工具调用增量"""
    index: int
    id: str | None = None
    name: str | None = None
    arguments: str = ""


@dataclass
class LLMMessage:
    """一条 LLM 消息"""
    role: Literal["system", "user", "assistant", "tool"]
    content: str | list[dict[str, Any]] | None = None
    reasoning_content: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None
    name: str | None = None


@dataclass
class LLMStreamChunk:
    """流式响应中的一个 chunk"""
    content: str | None = None
    reasoning_content: str | None = None
    tool_call_delta: ToolCallDelta | None = None
    finish_reason: str | None = None


@dataclass
class LLMResponse:
    """一次完整的 LLM 响应"""
    content: str = ""
    reasoning_content: str = ""
    tool_calls: list[FunctionCall] = field(default_factory=list)
    finish_reason: str = "stop"
    usage: dict[str, int] = field(default_factory=dict)

    def to_message(self) -> LLMMessage:
        msg = LLMMessage(
            role="assistant",
            content=self.content or None,
            reasoning_content=self.reasoning_content or None,
        )
        if self.tool_calls:
            msg.tool_calls = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": tc.arguments},
                }
                for tc in self.tool_calls
            ]
        return msg


@dataclass
class ToolResult:
    """工具执行结果"""
    tool_call_id: str
    name: str
    success: bool
    output: str
    error: str | None = None

    def to_message(self) -> LLMMessage:
        content = self.output if self.success else f"Error: {self.error}"
        return LLMMessage(
            role="tool",
            content=content[:8000],  # 截断过长结果
            tool_call_id=self.tool_call_id,
            name=self.name,
        )
