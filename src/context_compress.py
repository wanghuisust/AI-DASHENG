"""上下文压缩 — 对话过长时自动摘要，节省 token

策略：
  1. 当消息 token 估算超过阈值时，将较早的消息压缩为摘要
  2. 最近 N 条消息保持原样不压缩
"""

from langchain_core.messages import SystemMessage, HumanMessage
from constants import estimate_tokens, MAX_CONTEXT_TOKENS

COMPRESS_THRESHOLD = int(MAX_CONTEXT_TOKENS * 0.7)  # 35000
KEEP_RECENT = 6


def _messages_to_text(messages: list) -> str:
    """把消息列表转为可读文本"""
    lines = []
    for msg in messages:
        role = getattr(msg, "type", "unknown")
        content = ""
        if hasattr(msg, "content") and msg.content:
            content = msg.content if isinstance(msg.content, str) else str(msg.content)

        if role == "human":
            lines.append(f"用户: {content}")
        elif role == "ai":
            if hasattr(msg, "tool_calls") and msg.tool_calls:
                for tc in msg.tool_calls:
                    lines.append(f"AI调用工具: {tc['name']}({tc['args']})")
            if content:
                lines.append(f"AI: {content}")
        elif role == "tool":
            name = getattr(msg, "name", "tool")
            lines.append(f"工具[{name}]返回: {content[:500]}")
        else:
            lines.append(f"{role}: {content}")

    return "\n".join(lines)


def compress_messages(messages: list, llm=None) -> list:
    """压缩消息列表：旧消息 → 摘要，最近消息保持原样"""
    if len(messages) <= KEEP_RECENT:
        return messages

    total = sum(estimate_tokens(getattr(m, "content", "") or "") + 10 for m in messages)
    if total < COMPRESS_THRESHOLD:
        return messages

    old_msgs = messages[:-KEEP_RECENT]
    recent_msgs = messages[-KEEP_RECENT:]

    if llm is not None:
        try:
            text = _messages_to_text(old_msgs)
            if len(text) > 8000:
                text = text[:8000] + "\n...(已截断)"

            summary_prompt = SystemMessage(content=(
                "你是一个对话摘要助手。请将以下对话历史压缩为简洁的摘要，"
                "保留：1)用户的核心需求和意图 2)重要的工具调用及结果 3)已得出的结论\n"
                "摘要要简洁，不超过500字。"
            ))
            summary_response = llm.invoke([
                summary_prompt,
                HumanMessage(content=f"请摘要以下对话：\n\n{text}")
            ])
            summary_text = summary_response.content
        except Exception:
            summary_text = _simple_summarize(old_msgs)
    else:
        summary_text = _simple_summarize(old_msgs)

    compressed = [SystemMessage(content=f"[历史对话摘要]\n{summary_text}")]
    compressed.extend(recent_msgs)
    return compressed


def _simple_summarize(messages: list) -> str:
    """简单摘要：只保留用户消息和 AI 回复的关键行"""
    lines = []
    for msg in messages:
        role = getattr(msg, "type", "unknown")
        content = getattr(msg, "content", "") or ""
        if role == "human":
            lines.append(f"用户问: {content[:100]}")
        elif role == "ai" and content and not (hasattr(msg, "tool_calls") and msg.tool_calls):
            lines.append(f"AI答: {content[:100]}")
    return "\n".join(lines[-20:])
