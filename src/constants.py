"""共享常量和工具函数，避免循环导入"""

# 最大上下文 token 数（llama.cpp n_ctx=64000，留 14k 给回复）
MAX_CONTEXT_TOKENS = 50000


def estimate_tokens(text: str) -> int:
    """粗略估算 token 数
    中文 1 字 ≈ 1.5 token
    英文 1 字符 ≈ 0.4 token
    """
    if not text:
        return 0
    cn = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
    other = len(text) - cn
    return int(cn * 1.5 + other * 0.4)


def trim_messages_to_tokens(msgs: list, max_tokens: int = MAX_CONTEXT_TOKENS) -> list:
    """从最新消息开始保留，确保总 token 不超限"""
    total = 0
    kept = []
    for msg in reversed(msgs):
        content = ""
        if hasattr(msg, 'content') and msg.content:
            content = msg.content if isinstance(msg.content, str) else str(msg.content)
        if hasattr(msg, 'tool_calls') and msg.tool_calls:
            content += str(msg.tool_calls)
        if hasattr(msg, 'name') and msg.name == 'tool':
            content = str(content) * 2  # 工具输出双倍估算

        msg_tokens = estimate_tokens(content) + 10  # 消息格式开销
        if total + msg_tokens > max_tokens:
            break
        total += msg_tokens
        kept.append(msg)

    kept.reverse()
    return kept
