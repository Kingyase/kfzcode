"""上下文窗口管理 — 自动压缩超长对话"""
from ..llm.types import LLMMessage, extract_text_from_content, count_images_in_content


class ContextManager:
    """管理 Agent 的对话历史，防止超出 token 限制"""

    def __init__(self, max_tokens: int = 32000, reserve_for_response: int = 4000):
        self.max_tokens = max_tokens
        self.reserve_for_response = reserve_for_response
        self.effective_limit = max_tokens - reserve_for_response

    def estimate_tokens(self, messages: list[LLMMessage]) -> int:
        """粗略估算 token 数（中文按字数，英文按 4 char/token）
        多模态消息：text 块按文本估算，每张图片约 85 tokens"""
        total = 0
        for msg in messages:
            # 提取纯文本（多模态内容仅提取 text 块）
            text = extract_text_from_content(msg.content)
            # 粗略估计: 中文每字约1.5 token, 英文每4字符约1 token
            chinese_chars = sum(1 for c in text if '一' <= c <= '鿿')
            other_chars = len(text) - chinese_chars
            total += int(chinese_chars * 1.5 + other_chars / 4) + 50  # +50 for role overhead
            # 图片粗略估算: ~85 tokens/张（低细节模式）
            total += count_images_in_content(msg.content) * 85
        return total

    def should_compress(self, messages: list[LLMMessage]) -> bool:
        """判断是否需要压缩"""
        return self.estimate_tokens(messages) > self.effective_limit

    def compress(self, messages: list[LLMMessage]) -> list[LLMMessage]:
        """压缩对话历史 — 保留 system prompt + 最近的消息

        修复: 确保 tool_calls ↔ tool_result 配对被截断边界破坏，
        否则会导致 LLM API 返回 400 错误。
        """
        if not self.should_compress(messages):
            return messages

        # 保留 system prompt
        system_msgs = [m for m in messages if m.role == "system"]
        other_msgs = [m for m in messages if m.role != "system"]

        # 如果消息少，不需要压缩
        if len(other_msgs) <= 12:
            return messages

        # 保留最近 ~6 轮完整对话 + 之前消息的摘要
        recent = other_msgs[-12:]
        older = other_msgs[:-12]

        # 修复 tool_calls ↔ tool_result 配对被截断
        recent, older = self._repair_pairing(recent, older)

        # 生成摘要
        summary = self._generate_summary(older)
        summary_msg = LLMMessage(
            role="user",
            content=f"[对话历史摘要] 之前的对话中讨论了以下内容: {summary}",
        )

        return system_msgs + [summary_msg] + recent

    def _repair_pairing(
        self, recent: list[LLMMessage], older: list[LLMMessage]
    ) -> tuple[list[LLMMessage], list[LLMMessage]]:
        """修复 recent/older 边界处的 tool_calls ↔ tool_result 配对

        三种破坏场景:
        A. recent[0] 是 tool 消息，但其对应的 assistant(tool_calls) 在 older 中
           → 向前扩展 recent 直到包含该 assistant
        B. recent 内部的 assistant(tool_calls) 缺少部分 tool 结果
           → 从 older 末尾拉取缺失的 tool 结果
        C. recent 内部某个 assistant(tool_calls) 完全缺少 tool 结果且 older 中也没有
           → 移除该悬空 assistant 消息
        """

        # ---- 场景 A: recent 以孤儿 tool 消息开头 ----
        while recent and recent[0].role == "tool" and older:
            recent.insert(0, older.pop())

        # 经过上面的扩展后，recent[0] 如果是 assistant(tool_calls)，
        # 但仍需检查它的所有 tool 结果是否都在 recent 中
        if recent and recent[0].role == "assistant" and recent[0].tool_calls:
            expected = _get_tool_call_ids(recent[0])
            found = _collect_trailing_tool_ids(recent, start=1)
            missing = expected - found
            # 从 older 末尾拉取缺失的 tool 结果
            # 插入位置：紧接 assistant(tool_calls) 之后（tool 结果必须在 assistant 之后）
            insert_at = 1
            while older and missing:
                if older[-1].role == "tool" and older[-1].tool_call_id in missing:
                    m = older.pop()
                    missing.discard(m.tool_call_id)
                    recent.insert(insert_at, m)
                    insert_at += 1
                else:
                    break
            # 仍缺失 → 移除悬空的 assistant(tool_calls)
            if missing:
                recent.pop(0)

        # ---- 场景 B & C: 扫描 recent 内部的 assistant(tool_calls) 完整性 ----
        i = len(recent) - 1
        while i >= 0:
            msg = recent[i]
            if msg.role == "assistant" and msg.tool_calls:
                expected = _get_tool_call_ids(msg)
                found = _collect_trailing_tool_ids(recent, start=i + 1)
                missing = expected - found

                if missing:
                    # 尝试从 older 末尾拉取（仅对最后一条 assistant(tool_calls) 有意义）
                    pulled = []
                    while older and missing:
                        if older[-1].role == "tool" and older[-1].tool_call_id in missing:
                            m = older.pop()
                            missing.discard(m.tool_call_id)
                            pulled.append(m)
                        else:
                            break

                    if missing:
                        # 无法补全 — 移除悬空的 assistant(tool_calls)
                        # 以及紧跟它的部分 tool 结果
                        j = i + 1
                        while j < len(recent) and recent[j].role == "tool":
                            # 放回 older 末尾用于摘要（不影响配对检查，
                            #  因为已经没有对应的 tool_calls 了）
                            older.append(recent.pop(j))
                        recent.pop(i)  # 移除 assistant(tool_calls)
                    else:
                        # 成功补全 — 插入拉取的结果
                        insert_pos = i + 1
                        while insert_pos < len(recent) and recent[insert_pos].role == "tool":
                            insert_pos += 1
                        for m in reversed(pulled):
                            recent.insert(insert_pos, m)
            i -= 1

        return recent, older

    def _generate_summary(self, messages: list[LLMMessage]) -> str:
        """生成对话摘要（多模态消息提取文本 + 图片计数 + 文件/错误追踪）"""
        import json
        topics = []
        files_touched: set[str] = set()
        errors_seen: list[str] = []
        for msg in messages:
            if msg.role == "user":
                text = extract_text_from_content(msg.content)
                if not text:
                    continue
                short = text[:100].replace("\n", " ")
                img_count = count_images_in_content(msg.content)
                if img_count > 0:
                    short += f" [附带 {img_count} 张图片]"
                topics.append(f"- 用户: {short}")
            elif msg.role == "assistant":
                text = extract_text_from_content(msg.content)
                if msg.tool_calls:
                    tools = []
                    for tc in msg.tool_calls:
                        name = tc.get("function", {}).get("name", "?") if isinstance(tc, dict) else getattr(tc, "name", "?")
                        tools.append(name)
                        # 提取文件路径（read_file / write_file / edit_file 等工具的常见参数）
                        try:
                            raw_args = tc.get("function", {}).get("arguments", "{}") if isinstance(tc, dict) else getattr(tc, "arguments", "{}")
                            args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                            for key in ("file_path", "path", "working_dir"):
                                if key in args and args[key]:
                                    files_touched.add(str(args[key]))
                        except (json.JSONDecodeError, TypeError, AttributeError):
                            pass
                    topics.append(f"- 助手: 调用了工具 {', '.join(tools)}")
                elif text:
                    short = text[:100].replace("\n", " ")
                    topics.append(f"- 助手: {short}")
            elif msg.role == "tool":
                # 提取工具执行结果中的错误信息
                text = extract_text_from_content(msg.content)
                if text and ("error" in text.lower() or "Error" in text or "失败" in text or "failed" in text):
                    error_short = text[:120].replace("\n", " ")
                    errors_seen.append(error_short)

        # 构建最终摘要
        parts = ["\n".join(topics[:15])]
        if files_touched:
            files_list = ", ".join(sorted(files_touched)[:10])
            if len(files_touched) > 10:
                files_list += f" ... 等 {len(files_touched)} 个文件"
            parts.append(f"[涉及文件: {files_list}]")
        if errors_seen:
            parts.append(f"[关键错误: {'; '.join(errors_seen[-3:])}]")

        return "\n".join(parts)


def _get_tool_call_ids(msg: "LLMMessage") -> set[str]:
    """从一条 assistant 消息中提取所有 tool_call id"""
    ids: set[str] = set()
    if msg.tool_calls:
        for tc in msg.tool_calls:
            tc_id = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
            if tc_id:
                ids.add(tc_id)
    return ids


def _collect_trailing_tool_ids(messages: list["LLMMessage"], start: int,
                              skip: set[int] | None = None) -> set[str]:
    """从 start 位置开始收集连续 tool 消息的 tool_call_id"""
    ids: set[str] = set()
    skip = skip or set()
    i = start
    while i < len(messages) and messages[i].role == "tool":
        if i not in skip and messages[i].tool_call_id:
            ids.add(messages[i].tool_call_id)
        i += 1
    return ids


def repair_message_history(messages: list["LLMMessage"]) -> list["LLMMessage"]:
    """修复整个消息历史中的 tool_calls ↔ tool_result 配对

    全量扫描所有 assistant(tool_calls) 消息，验证其后紧跟的 tool 消息
    是否包含全部对应的 tool_call_id。对于无法补全的配对，移除悬空的
    assistant(tool_calls) 以及它之后的部分 tool 结果。

    三种损坏场景：
    1. assistant(tool_calls) 在末尾，无任何 tool 结果 → 移除它
    2. assistant(tool_calls) 后有部分 tool 结果，但缺少一部分 → 移除 assistant + 孤儿 tool
    3. assistant(tool_calls) 后有 tool 结果，但中间插入了其他消息 → 不处理（正常）

    OpenAI 兼容 API 要求：含 tool_calls 的 assistant 消息后必须紧跟
    等量的 tool 消息，否则返回 400。
    """
    if not messages:
        return messages

    result = list(messages)
    removed_indices: set[int] = set()

    i = len(result) - 1
    while i >= 0:
        if i in removed_indices:
            i -= 1
            continue
        msg = result[i]
        if msg.role == "assistant" and msg.tool_calls:
            expected = _get_tool_call_ids(msg)
            found = _collect_trailing_tool_ids(result, start=i + 1, skip=removed_indices)
            missing = expected - found

            if missing:
                # 移除悬空的 assistant(tool_calls)
                removed_indices.add(i)
                # 移除紧跟它的孤立 tool 结果（仅限连续的 tool 消息块）
                j = i + 1
                while j < len(result):
                    if j in removed_indices:
                        j += 1
                        continue
                    if result[j].role == "tool":
                        removed_indices.add(j)
                        j += 1
                    else:
                        break
        i -= 1

    if removed_indices:
        result = [m for idx, m in enumerate(result) if idx not in removed_indices]

    return result


def truncate_content(content: str, max_chars: int = 12000) -> str:
    """截断过长内容"""
    if len(content) <= max_chars:
        return content
    half = max_chars // 2
    return (
        content[:half]
        + f"\n\n... [中间省略 {len(content) - max_chars} 字符] ...\n\n"
        + content[-half:]
    )
