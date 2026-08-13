"""DeepV4 OpenAI 兼容 LLM 客户端"""
import json
import asyncio
import time
import logging
from typing import AsyncIterator

import httpx

from .types import LLMResponse, LLMStreamChunk, ToolCallDelta, LLMMessage, ToolDefinition

logger = logging.getLogger(__name__)


class LLMRetryableError(Exception):
    """可重试的 LLM 请求错误（限流、服务暂时不可用等）"""
    pass


class LLMFatalError(Exception):
    """不可重试的 LLM 请求错误（如 400 Bad Request）"""
    pass


class DeepV4Client:
    """OpenAI 兼容 LLM 客户端 — 支持流式 + reasoning_content + 自动重试
    兼容所有 OpenAI 兼容 API (DeepSeek, 智谱 GLM, DeepV4 等)"""

    RETRYABLE_STATUSES = {429, 502, 503, 504}
    MAX_RETRIES = 3
    RETRY_BASE_DELAY = 4.0   # 指数退避基础延迟
    RETRY_MAX_DELAY = 120.0  # 最大退避延迟

    def __init__(self, base_url: str, api_key: str = "",
                 model: str = "deepv4-large",
                 max_tokens: int = 8192,
                 temperature: float = 0.7,
                 top_p: float = 0.95):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.top_p = top_p
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(300.0),
                trust_env=False,  # 禁用系统代理，避免 Privoxy 等本地代理干扰
            )
        return self._client

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    def _should_retry(self, status_code: int | None, error: Exception | None) -> bool:
        """判断是否应该重试"""
        if status_code and status_code in self.RETRYABLE_STATUSES:
            return True
        if error:
            # 连接错误、超时等都应重试
            if isinstance(error, (httpx.ConnectError, httpx.RemoteProtocolError,
                                  httpx.ReadError, httpx.WriteError,
                                  httpx.PoolTimeout, httpx.ConnectTimeout,
                                  httpx.ReadTimeout)):
                return True
        return False

    def _calc_delay(self, attempt: int) -> float:
        """指数退避延迟"""
        return min(self.RETRY_MAX_DELAY, self.RETRY_BASE_DELAY * (2 ** attempt))

    async def _retry_request(
        self, fn, *args, **kwargs
    ) -> httpx.Response:
        """执行带重试的 HTTP 请求"""
        last_error = None
        for attempt in range(self.MAX_RETRIES + 1):
            try:
                resp = await fn(*args, **kwargs)
                if resp.status_code in self.RETRYABLE_STATUSES and attempt < self.MAX_RETRIES:
                    delay = self._calc_delay(attempt)
                    logger.warning(
                        f"LLM API 返回 {resp.status_code}，{delay:.1f}s 后重试 (attempt {attempt + 1}/{self.MAX_RETRIES})"
                    )
                    await asyncio.sleep(delay)
                    continue
                return resp
            except (httpx.ConnectError, httpx.RemoteProtocolError,
                    httpx.ReadError, httpx.WriteError,
                    httpx.PoolTimeout, httpx.ConnectTimeout,
                    httpx.ReadTimeout) as e:
                last_error = e
                if attempt < self.MAX_RETRIES:
                    delay = self._calc_delay(attempt)
                    logger.warning(
                        f"LLM API 连接错误: {e}，{delay:.1f}s 后重试 (attempt {attempt + 1}/{self.MAX_RETRIES})"
                    )
                    await asyncio.sleep(delay)
                    continue
                raise
        raise last_error or RuntimeError("Retry exhausted")


    async def chat(
        self,
        messages: list[LLMMessage],
        tools: list[ToolDefinition] | None = None,
        stream: bool = True,
    ) -> LLMResponse:
        """发送聊天请求"""
        if stream:
            return await self._chat_stream(messages, tools)
        return await self._chat_sync(messages, tools)

    async def _chat_sync(
        self, messages: list[LLMMessage], tools: list[ToolDefinition] | None
    ) -> LLMResponse:
        """非流式请求（带重试）"""
        client = await self._get_client()
        body = self._build_body(messages, tools, stream=False)
        headers = self._build_headers()

        resp = await self._retry_request(
            client.post, f"{self.base_url}/chat/completions", json=body, headers=headers
        )
        resp.raise_for_status()
        data = resp.json()

        choice = data["choices"][0]
        return LLMResponse(
            content=choice["message"].get("content", "") or "",
            finish_reason=choice.get("finish_reason", "stop"),
            usage=data.get("usage", {}),
        )

    async def _chat_stream(
        self, messages: list[LLMMessage], tools: list[ToolDefinition] | None
    ) -> LLMResponse:
        """流式请求 — 委托给 _read_stream_lines 处理（ReadError 已在内部降级）"""
        client = await self._get_client()
        body = self._build_body(messages, tools, stream=True)
        headers = self._build_headers()

        response = LLMResponse()
        current_tool_deltas: dict[int, ToolCallDelta] = {}
        any_data_received = False

        try:
            async for chunk in self._read_stream_lines(client, body, headers):
                any_data_received = True
                if chunk.reasoning_content:
                    response.reasoning_content += chunk.reasoning_content
                if chunk.content:
                    response.content += chunk.content
                if chunk.tool_call_delta:
                    td = chunk.tool_call_delta
                    if td.index not in current_tool_deltas:
                        current_tool_deltas[td.index] = ToolCallDelta(index=td.index)
                    cur = current_tool_deltas[td.index]
                    if td.id:
                        cur.id = td.id
                    if td.name:
                        cur.name = td.name
                    if td.arguments:
                        cur.arguments += td.arguments
                if chunk.finish_reason:
                    response.finish_reason = chunk.finish_reason
        except httpx.ReadError:
            # _read_stream_lines 只在零数据时 re-raise，此时才是真正失败
            if any_data_received:
                if not response.finish_reason:
                    response.finish_reason = "stop"
            else:
                raise

        # 合并 tool_call deltas
        from .types import FunctionCall
        for delta in sorted(current_tool_deltas.values(), key=lambda d: d.index):
            if delta.name:
                response.tool_calls.append(FunctionCall(
                    id=delta.id or "",
                    name=delta.name,
                    arguments=delta.arguments,
                ))

        return response

    async def _read_stream_lines(self, client: httpx.AsyncClient,
                                  body: dict, headers: dict) -> AsyncIterator[LLMStreamChunk]:
        """读取一次流式响应的所有行，逐 chunk yield

        注意：DeepSeek 等 API 在流式传输完成时可能主动断开连接导致 ReadError。
        此时已接收的内容仍然有效，不应报错。"""
        any_chunk_yielded = False
        try:
            async with client.stream(
                "POST", f"{self.base_url}/chat/completions",
                json=body, headers=headers
            ) as resp:
                if resp.status_code >= 400:
                    # 在 response 存活时立即读取错误体
                    error_text = ""
                    try:
                        error_body = await resp.aread()
                        error_text = error_body.decode("utf-8", errors="replace")[:1000]
                    except Exception:
                        error_text = "(无法读取响应体)"
                    logger.error(f"API {resp.status_code} 错误:\n{error_text}")
                    msg_count = len(body.get("messages", []))
                    logger.error(f"请求 messages 数量: {msg_count}")
                    raise IOError(f"HTTP {resp.status_code}: {error_text}")
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data_str = line[6:]
                    if data_str == "[DONE]":
                        break

                    try:
                        chunk_data = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue

                    delta = chunk_data.get("choices", [{}])[0].get("delta", {})
                    finish = chunk_data.get("choices", [{}])[0].get("finish_reason")

                    reasoning = delta.get("reasoning_content", "")
                    content = delta.get("content", "")

                    # 解析工具调用 delta（一个 SSE delta 可能包含多个 tool_calls）
                    tool_call_deltas: list[ToolCallDelta] = []
                    for tc_delta in delta.get("tool_calls", []):
                        func = tc_delta.get("function", {})
                        tool_call_deltas.append(ToolCallDelta(
                            index=tc_delta.get("index", 0),
                            id=tc_delta.get("id"),
                            name=func.get("name"),
                            arguments=func.get("arguments", ""),
                        ))

                    has_data = reasoning or content or tool_call_deltas or finish
                    if has_data:
                        any_chunk_yielded = True
                        if tool_call_deltas:
                            # 每个 tool_call delta 单独 yield，text 仅放在第一个
                            first = True
                            for td in tool_call_deltas:
                                yield LLMStreamChunk(
                                    content=content if first else None,
                                    reasoning_content=reasoning if first else None,
                                    tool_call_delta=td,
                                    finish_reason=finish if first else None,
                                )
                                first = False
                        else:
                            yield LLMStreamChunk(
                                content=content or None,
                                reasoning_content=reasoning or None,
                                tool_call_delta=None,
                                finish_reason=finish or None,
                            )
        except httpx.ReadError:
            if not any_chunk_yielded:
                raise  # 一个 chunk 都没收到 → 真正的连接失败

    async def chat_stream_generator(
        self, messages: list[LLMMessage], tools: list[ToolDefinition] | None = None,
    ) -> AsyncIterator[LLMStreamChunk]:
        """流式请求 — 逐 chunk yield（ReadError 已在 _read_stream_lines 内降级处理）"""
        client = await self._get_client()
        body = self._build_body(messages, tools, stream=True)
        headers = self._build_headers()

        try:
            async for chunk in self._read_stream_lines(client, body, headers):
                yield chunk
        except httpx.HTTPStatusError as e:
            # 在 response 还存活时捕获错误体，避免 engine 层读不到
            try:
                error_body = await e.response.aread()
                error_text = error_body.decode("utf-8", errors="replace")[:500]
            except Exception:
                error_text = "(无法读取响应体)"
            raise IOError(f"HTTP {e.response.status_code}: {error_text}") from None

    def _build_body(
        self, messages: list[LLMMessage], tools: list[ToolDefinition] | None, stream: bool
    ) -> dict:
        """构建请求体"""
        body: dict = {
            "model": self.model,
            "messages": [self._serialize_msg(m) for m in messages],
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "stream": stream,
        }
        if tools:
            body["tools"] = [
                {"type": "function", "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                }}
                for t in tools
            ]
        return body

    def _serialize_msg(self, msg: LLMMessage) -> dict:
        """序列化消息 — 支持纯文本和多模态 (content 数组) 两种格式"""
        result: dict = {"role": msg.role}
        # content: 支持纯文本字符串 或 多模态 content 数组
        if msg.content is not None:
            if isinstance(msg.content, list):
                result["content"] = msg.content   # 多模态数组直接透传
            else:
                result["content"] = msg.content   # 纯文本字符串
        elif msg.role == "assistant" and not msg.tool_calls:
            result["content"] = ""
        # reasoning_content: thinking 模式模型（如 deepseek-v4-pro / deepseek-reasoner）
        # 要求把 assistant 消息的推理内容原样传回，否则下一轮请求会返回 400
        if msg.reasoning_content:
            result["reasoning_content"] = msg.reasoning_content
        # tool_calls
        if msg.tool_calls:
            result["tool_calls"] = msg.tool_calls
        # tool_call_id: tool 角色消息必须有此字段
        if msg.tool_call_id is not None:
            result["tool_call_id"] = msg.tool_call_id
        # name: tool 角色消息通常需要此字段
        if msg.name is not None:
            result["name"] = msg.name
        return result

    def _build_headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    async def health_check(self) -> dict:
        """检查 API 连通性 — 先尝试 /models 端点，若不支持则退回到基础连通性探测"""
        import time
        client = await self._get_client()
        start = time.monotonic()
        try:
            resp = await client.get(f"{self.base_url}/models", headers=self._build_headers())
            elapsed = (time.monotonic() - start) * 1000
            if resp.status_code == 200:
                return {"ok": True, "latency_ms": round(elapsed, 1)}
            # 部分 provider 不支持 /models 端点（如智谱），尝试 chat completions 的轻量探测
            if resp.status_code in (404, 405):
                # 回退：用最小请求探测 chat endpoint
                import time as t2
                t2_start = t2.monotonic()
                ping_body = {
                    "model": self.model,
                    "messages": [{"role": "user", "content": "ping"}],
                    "max_tokens": 1,
                    "stream": False,
                }
                resp2 = await client.post(
                    f"{self.base_url}/chat/completions",
                    json=ping_body, headers=self._build_headers()
                )
                t2_elapsed = (t2.monotonic() - t2_start) * 1000
                ok = resp2.status_code in (200, 400)  # 400 也说明端点在响应
                return {"ok": ok, "latency_ms": round(t2_elapsed, 1), "endpoint": "chat/completions"}
            return {"ok": False, "latency_ms": round(elapsed, 1), "status": resp.status_code}
        except Exception as e:
            return {"ok": False, "error": str(e)}
