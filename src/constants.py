"""共享常量和工具函数，避免循环导入"""

import os


def _get_model_context_length() -> int:
    """延迟读取 MODEL_CONTEXT_LENGTH，确保 load_dotenv 已执行"""
    return int(os.environ.get("MODEL_CONTEXT_LENGTH", "256000"))


# 最大上下文 token 数（用于 trim 和压缩判断）— 延迟读取
def get_max_context_tokens() -> int:
    return _get_model_context_length()


# 上下文压缩阈值：达到上下文长度的 50% 时自动压缩
def get_compress_threshold() -> int:
    return int(_get_model_context_length() * 0.5)


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


def trim_messages_to_tokens(msgs: list, max_tokens: int = None) -> list:
    """从最新消息开始保留，确保总 token 不超限"""
    if max_tokens is None:
        max_tokens = get_max_context_tokens()
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

        msg_tokens = estimate_tokens(content) + 10
        if total + msg_tokens > max_tokens:
            break
        total += msg_tokens
        kept.append(msg)

    kept.reverse()
    return kept
