"""上下文压缩 — 对话过长时自动摘要，节省 token

融合 Hermes Agent 的精确压缩策略 + Dasheng 的工具预处理和增量摘要：

策略：
  Phase 1: 工具输出预处理 — 去重、分类截断冗长的工具返回  [Dasheng保留]
  Phase 2: 受保护区域标记 — 第一条消息 + 最后N条  [借鉴Hermes]
  Phase 3: 边界保护 — 防止切断 tool response 对  [借鉴Hermes]
  Phase 4: 基于token预算的压缩区域计算  [借鉴Hermes]
  Phase 5: LLM 结构化摘要（增量更新）+ 重试容错  [融合两者]
  Phase 6: 回退 — 简单摘要兜底  [Dasheng保留]
"""

import hashlib
import json as _json
import time
import random
import logging
from typing import List, Tuple, Optional
from langchain_core.messages import (
    SystemMessage, HumanMessage, AIMessage, ToolMessage,
)
from constants import estimate_tokens, get_compress_threshold, get_max_context_tokens

logger = logging.getLogger(__name__)

# ── 可调参数 ──
TAIL_TOKEN_RATIO = 0.3       # 尾部保留区占总预算的比例 (Hermes: ~30%)
MIN_TAIL_MESSAGES = 4        # 尾部最少保留的消息条数
PROTECT_LAST_N = 4           # 保护最后N条turn (Hermes默认)
SUMMARY_TARGET_TOKENS = 750  # 摘要目标token数
SUMMARY_MAX_CHARS = 3000     # 摘要最大字符数
TOOL_OUTPUT_MAX_CHARS = 500  # 工具输出预处理后最大字符数
SUMMARY_MAX_RETRIES = 3      # 摘要生成最大重试次数
SUMMARY_BASE_DELAY = 2.0     # 摘要重试基础延迟(秒)
CONTENT_TRUNCATE_CHARS = 3000 # 摘要prompt内单turn内容截断长度
CONTENT_TRUNCATE_HEAD = 1500 # 截断时保留头部字符数
CONTENT_TRUNCATE_TAIL = 500  # 截断时保留尾部字符数

SUMMARY_PREFIX = "[CONTEXT SUMMARY]"


# ═══════════════════════════════════════════════════════════
# Phase 1: 工具输出预处理 (Dasheng原有，保留)
# ═══════════════════════════════════════════════════════════

def _content_hash(text: str) -> str:
    """短哈希，用于工具输出去重"""
    return hashlib.md5(text[:1000].encode()).hexdigest()[:8]


def _summarize_tool_output(name: str, content: str) -> str:
    """将冗长的工具输出压缩为一行摘要"""
    if not content:
        return content
    if len(content) <= TOOL_OUTPUT_MAX_CHARS:
        return content

    name_lower = (name or "").lower()

    if any(kw in name_lower for kw in ["search", "find", "list", "glob", "ls"]):
        lines = content.strip().split("\n")
        kept = "\n".join(lines[:8])
        return f"{kept}\n... (共 {len(lines)} 行，已省略)"

    if any(kw in name_lower for kw in ["read", "cat", "head"]):
        lines = content.strip().split("\n")
        kept = "\n".join(lines[:10])
        return f"{kept}\n... (共 {len(lines)} 行，已省略)"

    if any(kw in name_lower for kw in ["terminal", "shell", "exec", "bash"]):
        lines = content.strip().split("\n")
        if len(lines) <= 15:
            return content
        kept = "\n".join(lines[:3] + ["..."] + lines[-5:])
        return f"{kept}\n... (共 {len(lines)} 行，已省略)"

    return content[:TOOL_OUTPUT_MAX_CHARS] + "\n... (已截断)"


def _smart_truncate(content: str, name: str) -> str:
    """智能截断：保留首尾，标注省略量（参考Claude Code FileReadTool多层截断）

    核心原则：关键信息不丢失，LLM能用offset/limit续读
    """
    if not content or len(content) <= TOOL_OUTPUT_MAX_CHARS:
        return content

    HEAD = 2000
    TAIL = 2000
    name_lower = (name or "").lower()

    # 搜索类工具：保留更多头部（文件列表最重要）
    if any(kw in name_lower for kw in ["search", "find", "glob", "ls"]):
        HEAD = 3000
        TAIL = 1000

    # 读取类工具：保留行号信息，提示offset续读
    if any(kw in name_lower for kw in ["read", "cat", "head"]):
        lines = content.strip().split("\n")
        if len(lines) <= 100:
            return content  # 行数不多就不截断
        head_lines = lines[:30]
        tail_lines = lines[-10:]
        omitted_lines = len(lines) - 40
        return (
            "\n".join(head_lines)
            + f"\n\n... [已省略 {omitted_lines} 行，可用 offset/limit 参数读取指定部分] ...\n\n"
            + "\n".join(tail_lines)
        )

    # 终端类工具：首尾保留
    if any(kw in name_lower for kw in ["terminal", "shell", "exec", "bash"]):
        lines = content.strip().split("\n")
        if len(lines) <= 50:
            # 行数不多但字符超限，直接截断
            if len(content) <= TOOL_OUTPUT_MAX_CHARS:
                return content
            return content[:TOOL_OUTPUT_MAX_CHARS] + "\n... [输出已截断]"

        head_lines = lines[:10]
        tail_lines = lines[-8:]
        omitted_lines = len(lines) - 18
        return (
            "\n".join(head_lines)
            + f"\n\n... [已省略 {omitted_lines} 行] ...\n\n"
            + "\n".join(tail_lines)
        )

    # 通用截断：保留首尾字符
    head = content[:HEAD]
    tail = content[-TAIL:]
    omitted = len(content) - HEAD - TAIL
    return (
        head
        + f"\n\n... [已省略 {omitted:,} 字符] ...\n\n"
        + tail
    )


def _prune_tool_outputs(messages: list) -> list:
    """预处理工具输出：智能截断冗长返回（不再用去重标记）

    改造说明（参考Claude Code设计）：
    - 去重标记"(同第X条消息)"让LLM看不到完整结果，导致重复调用
    - 改为智能截断：首尾保留+省略提示，LLM能看到关键信息
    - 去重由guardrail在调用前判断（已实现），不需要在结果层做
    """
    result = []

    for i, msg in enumerate(messages):
        if not (hasattr(msg, "type") and msg.type == "tool"):
            result.append(msg)
            continue

        tool_name = getattr(msg, "name", "tool")
        content = msg.content if isinstance(msg.content, str) else str(msg.content or "")

        # 智能截断（替代去重标记）
        trimmed = _smart_truncate(content, tool_name)
        if trimmed != content:
            new_msg = ToolMessage(
                content=trimmed,
                tool_call_id=getattr(msg, "tool_call_id", ""),
                name=tool_name,
            )
            result.append(new_msg)
        else:
            result.append(msg)

    return result


# ═══════════════════════════════════════════════════════════
# Phase 2: 受保护区域标记 (借鉴Hermes)
# ═══════════════════════════════════════════════════════════

def _find_protected_indices(messages: list) -> Tuple[set, int, int]:
    """
    找出受保护的消息索引。

    保护：
    - 第一条 system / human / ai / tool 消息
    - 最后 N 条消息

    Returns:
        (protected_set, compress_start, compress_end)
        compressible region = messages[compress_start:compress_end]
    """
    n = len(messages)
    protected = set()

    first_system = first_human = first_ai = first_tool = None

    for i, msg in enumerate(messages):
        role = getattr(msg, "type", "")
        if role == "system" and first_system is None:
            first_system = i
        elif role == "human" and first_human is None:
            first_human = i
        elif role == "ai" and first_ai is None:
            first_ai = i
        elif role == "tool" and first_tool is None:
            first_tool = i

    # 保护第一条各类型消息
    if first_system is not None:
        protected.add(first_system)
    if first_human is not None:
        protected.add(first_human)
    if first_ai is not None:
        protected.add(first_ai)
    if first_tool is not None:
        protected.add(first_tool)

    # 保护最后N条
    for i in range(max(0, n - PROTECT_LAST_N), n):
        protected.add(i)

    # 确定可压缩区域
    half = n // 2
    head_protected = sorted(i for i in protected if i < half)
    tail_protected = sorted(i for i in protected if i >= half)

    compress_start = max(head_protected) + 1 if head_protected else 0
    compress_end = min(tail_protected) if tail_protected else n

    return protected, compress_start, compress_end


# ═══════════════════════════════════════════════════════════
# Phase 3: 边界保护 (借鉴Hermes _snap_boundary)
# ═══════════════════════════════════════════════════════════

def _is_boundary_clean(messages: list, idx: int) -> bool:
    """
    边界在idx处是干净的，不会切断 tool response 对。

    在from/value格式中，tool turn 紧跟在其对应的ai turn之后。
    如果边界落在tool turn上，意味着把tool的response从上下文中切掉了。
    边界干净的条件：idx越界 或 idx处的消息不是tool turn。
    """
    return idx >= len(messages) or getattr(messages[idx], "type", "") != "tool"


def _snap_boundary(messages: list, idx: int, min_idx: int, max_idx: int) -> int:
    """
    将压缩边界移动到最近的干净边界。

    优先向前移动（把孤立的tool turn纳入压缩区），
    如果前方没有干净边界，则向后回退。
    """
    # 向前找干净边界
    forward = idx
    while forward < max_idx and not _is_boundary_clean(messages, forward):
        forward += 1
    if _is_boundary_clean(messages, forward):
        return forward

    # 向后回退找干净边界
    backward = idx
    while backward > min_idx and not _is_boundary_clean(messages, backward):
        backward -= 1
    return backward


# ═══════════════════════════════════════════════════════════
# Phase 4: Token计算 & 压缩区域选择 (借鉴Hermes)
# ═══════════════════════════════════════════════════════════

def _msg_tokens(msg) -> int:
    """估算单条消息的token数"""
    content = ""
    if hasattr(msg, "content") and msg.content:
        content = msg.content if isinstance(msg.content, str) else str(msg.content)
    if hasattr(msg, "tool_calls") and msg.tool_calls:
        content += _json.dumps(msg.tool_calls, ensure_ascii=False, default=str)
    return estimate_tokens(content) + 10


def _count_trajectory_tokens(messages: list) -> int:
    """计算总token数"""
    return sum(_msg_tokens(m) for m in messages)


def _extract_turn_content_for_summary(
    messages: list, start: int, end: int
) -> str:
    """提取待摘要的消息内容，用于LLM摘要prompt"""
    parts = []
    for i in range(start, end):
        msg = messages[i]
        role = getattr(msg, "type", "unknown")
        value = ""
        if hasattr(msg, "content") and msg.content:
            value = msg.content if isinstance(msg.content, str) else str(msg.content)
        # 借鉴Hermes: 长内容保留首尾
        if len(value) > CONTENT_TRUNCATE_CHARS:
            value = value[:CONTENT_TRUNCATE_HEAD] + "\n...[truncated]...\n" + value[-CONTENT_TRUNCATE_TAIL:]
        parts.append(f"[Turn {i} - {role.upper()}]:\n{value}")
    return "\n\n".join(parts)


# ═══════════════════════════════════════════════════════════
# Phase 5: LLM结构化摘要 + 重试容错 (融合两者)
# ═══════════════════════════════════════════════════════════

SUMMARY_TEMPLATE = """你是一个上下文压缩助手。你的任务是将对话历史压缩为结构化摘要。

重要规则：
- 你不能调用任何工具
- 你必须保留所有关键信息
- 你必须按以下模板输出

<analysis>
[分析当前对话状态：用户在做什么、进展如何、遇到了什么问题]
</analysis>

<summary>
Primary Request:
[用户的原始请求，原文保留]

Key Technical Concepts:
[涉及的关键技术概念、框架、库]

Files and Code Sections:
[列出所有涉及到的文件路径和关键代码段]
格式: - path/to/file: [简要说明文件角色和关键内容]

Errors Encountered:
[遇到的错误及其原因分析]

Problem Solving:
[已解决的问题和解决方法]

Pending Tasks:
[尚未完成的任务列表]

Current State:
[当前处于什么状态，正在做什么]

Next Steps:
[建议的下一步操作]
</summary>
---

TURNS TO SUMMARIZE:
{content}
---

Write only the summary, starting with "{summary_prefix}:" prefix.
Target approximately {summary_target_tokens} tokens."""

INCREMENTAL_TEMPLATE = """你是一个上下文压缩助手。下面有一段旧摘要和一段新的对话记录。
请将新信息合并到旧摘要中，严格按照以下结构化模板更新。

重要规则：
- 必须保留所有文件路径、错误信息、关键数值
- 每个字段1-3行，总长度不超过 {max_chars} 字符
- 按模板结构输出，不要自由格式

旧摘要：
{old_summary}

新对话记录：
{new_history}

请输出更新后的完整摘要（保持 <summary> 结构）：
"""

FALLBACK_SUMMARY = "[CONTEXT SUMMARY]: [摘要生成失败 — 之前的工具调用和响应已被压缩以节省上下文空间。]"


def _jittered_backoff(attempt: int, base_delay: float = SUMMARY_BASE_DELAY, max_delay: float = 30.0) -> float:
    """指数退避 + 抖动，防止并发重试风暴"""
    exponent = max(0, attempt - 1)
    delay = min(base_delay * (2 ** exponent), max_delay)
    jitter = random.uniform(0, 0.5 * delay)
    return delay + jitter


def _find_existing_summary(messages: list) -> tuple:
    """查找已有的摘要消息"""
    for i, msg in enumerate(messages):
        if hasattr(msg, "type") and msg.type == "system":
            content = msg.content if isinstance(msg.content, str) else str(msg.content or "")
            if SUMMARY_PREFIX in content:
                body = content.split(SUMMARY_PREFIX, 1)[-1].strip()
                return i, body
    return -1, ""


def _messages_to_text(messages: list) -> str:
    """把消息列表转为可读文本（用于增量摘要输入）"""
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
                    args_str = _json.dumps(tc.get("args", {}), ensure_ascii=False, default=str)
                    if len(args_str) > 200:
                        args_str = args_str[:200] + "..."
                    lines.append(f"AI调用工具: {tc.get('name', 'tool')}({args_str})")
            if content:
                lines.append(f"AI: {content[:500]}")
        elif role == "tool":
            name = getattr(msg, "name", "tool")
            lines.append(f"工具[{name}]返回: {content[:300]}")
        elif role == "system":
            lines.append(f"[旧摘要]: {content[:1000]}")
        else:
            lines.append(f"{role}: {content[:200]}")

    return "\n".join(lines)


def _generate_summary(
    content: str,
    llm,
    existing_summary: str = "",
    metrics: Optional[dict] = None,
) -> str:
    """
    生成摘要，带重试和容错。

    Args:
        content: 待摘要的消息文本
        llm: ChatOpenAI 实例
        existing_summary: 已有摘要（增量更新模式）
        metrics: 统计信息字典（可选）

    Returns:
        摘要文本
    """
    if metrics is None:
        metrics = {}

    for attempt in range(SUMMARY_MAX_RETRIES):
        try:
            metrics.setdefault("summarization_api_calls", 0)
            metrics["summarization_api_calls"] += 1

            if existing_summary:
                history_text = content
                if len(history_text) > 12000:
                    history_text = history_text[:12000] + "\n...(对话历史已截断)"
                prompt_text = INCREMENTAL_TEMPLATE.format(
                    old_summary=existing_summary,
                    new_history=history_text,
                    max_chars=SUMMARY_MAX_CHARS,
                )
            else:
                prompt_text = SUMMARY_TEMPLATE.format(
                    summary_target_tokens=SUMMARY_TARGET_TOKENS,
                    content=content,
                    summary_prefix=SUMMARY_PREFIX,
                )

            response = llm.invoke([
                SystemMessage(content="你是一个对话摘要助手。输出简洁的结构化摘要，使用中文。"),
                HumanMessage(content=prompt_text),
            ])
            summary = response.content if isinstance(response.content, str) else str(response.content)

            if len(summary) > SUMMARY_MAX_CHARS:
                summary = summary[:SUMMARY_MAX_CHARS] + "\n...(摘要已截断)"

            return summary

        except Exception as e:
            metrics.setdefault("summarization_errors", 0)
            metrics["summarization_errors"] += 1
            logger.warning(f"摘要生成尝试 {attempt + 1}/{SUMMARY_MAX_RETRIES} 失败: {e}")

            if attempt < SUMMARY_MAX_RETRIES - 1:
                time.sleep(_jittered_backoff(attempt + 1, base_delay=SUMMARY_BASE_DELAY))
            else:
                logger.warning("摘要生成全部失败，使用兜底文本")
                return FALLBACK_SUMMARY


# ═══════════════════════════════════════════════════════════
# Phase 6: 简单摘要回退 (Dasheng原有，保留)
# ═══════════════════════════════════════════════════════════

def _simple_summarize(messages: list) -> str:
    """简单摘要：无LLM时的兜底方案"""
    lines = []
    for msg in messages:
        role = getattr(msg, "type", "unknown")
        content = getattr(msg, "content", "") or ""
        if role == "system" and SUMMARY_PREFIX in content:
            body = content.split(SUMMARY_PREFIX, 1)[-1].strip()
            lines.append(f"[旧摘要]: {body[:500]}")
        elif role == "human":
            lines.append(f"用户问: {content[:100]}")
        elif role == "ai" and content and not (hasattr(msg, "tool_calls") and msg.tool_calls):
            lines.append(f"AI答: {content[:100]}")
        elif role == "ai" and hasattr(msg, "tool_calls") and msg.tool_calls:
            tool_names = ", ".join(tc.get("name", "?") for tc in msg.tool_calls)
            lines.append(f"AI调用: {tool_names}")
    return "\n".join(lines[-30:])


# ═══════════════════════════════════════════════════════════
# 主入口
# ═══════════════════════════════════════════════════════════

def compress_messages(messages: list, llm=None, metrics: Optional[dict] = None) -> list:
    """
    压缩消息列表（融合 Hermes + Dasheng 方案）。

    流程：
    1. 工具输出预处理（去重+截断）
    2. 检查是否需要压缩
    3. 找出受保护区域
    4. 计算压缩区域边界（带边界保护）
    5. 生成/更新摘要
    6. 组装结果

    Args:
        messages: 消息列表
        llm: ChatOpenAI 实例（可选，None时回退到简单摘要）
        metrics: 统计信息字典（可选）

    Returns:
        压缩后的消息列表
    """
    if metrics is None:
        metrics = {}

    original_count = len(messages)
    original_tokens = _count_trajectory_tokens(messages)
    metrics["original_tokens"] = original_tokens
    metrics["original_turns"] = original_count

    if original_count <= MIN_TAIL_MESSAGES:
        return messages

    # Phase 1: 工具输出预处理
    messages = _prune_tool_outputs(messages)

    original_tokens = _count_trajectory_tokens(messages)
    metrics["original_tokens"] = original_tokens
    metrics["pruned_tool_calls"] = original_count - len(messages)

    total_tokens = original_tokens
    threshold = get_compress_threshold()
    need_compress = total_tokens >= threshold or len(messages) >= 40
    if not need_compress:
        return messages

    # Phase 2: 找出受保护区域
    protected, compress_start, compress_end = _find_protected_indices(messages)

    # Phase 3: 边界保护
    compress_start = _snap_boundary(messages, compress_start, 0, compress_end)

    if compress_start >= compress_end:
        return messages

    # Phase 4: 计算压缩区域
    max_tokens = get_max_context_tokens()
    tail_budget = int(max_tokens * TAIL_TOKEN_RATIO)

    old_tokens = _count_trajectory_tokens(messages[:compress_start])
    tokens_to_compress = old_tokens - tail_budget
    if tokens_to_compress <= 0:
        return messages

    accumulated = 0
    split = compress_start
    for i in range(compress_start, compress_end):
        accumulated += _msg_tokens(messages[i])
        split = i + 1
        if accumulated >= tokens_to_compress:
            break

    if accumulated < tokens_to_compress:
        split = compress_end

    split = _snap_boundary(messages, split, compress_start, compress_end)
    if split <= compress_start:
        return messages

    content_to_summarize = _extract_turn_content_for_summary(messages, compress_start, split)

    # Phase 5: 查找已有摘要 + 生成
    existing_idx, existing_summary = _find_existing_summary(messages[:compress_start])

    if existing_idx >= 0:
        old_for_summary = messages[compress_start:existing_idx] + messages[existing_idx + 1:split]
    else:
        old_for_summary = messages[compress_start:split]

    if llm is not None:
        summary_text = _generate_summary(
            _messages_to_text(old_for_summary) if not existing_summary else content_to_summarize,
            llm,
            existing_summary=existing_summary,
            metrics=metrics,
        )
    else:
        simple = _simple_summarize(old_for_summary)
        if existing_summary:
            summary_text = f"{existing_summary}\n\n--- 新增 ---\n{simple}"
        else:
            summary_text = simple

    # Phase 6: 组装结果
    summary_msg = SystemMessage(content=f"{SUMMARY_PREFIX}: {summary_text}")
    result = [summary_msg] + messages[split:]

    compressed_tokens = _count_trajectory_tokens(result)
    metrics["compressed_tokens"] = compressed_tokens
    metrics["compressed_turns"] = len(result)
    metrics["tokens_saved"] = original_tokens - compressed_tokens
    metrics["compression_ratio"] = compressed_tokens / max(original_tokens, 1)
    metrics["turns_removed"] = len(result) - len(messages)

    logger.info(
        f"[COMPRESS] {original_count} msgs → {len(result)} msgs "
        f"({original_tokens}→{compressed_tokens} tokens, "
        f"ratio={metrics['compression_ratio']:.2%})"
    )

    return result


def compress_with_fallback(messages: list, llm=None) -> list:
    """
    带 fallback 的压缩入口。
    """
    if llm is None:
        return compress_messages(messages, llm=None)

    try:
        return compress_messages(messages, llm=llm)
    except Exception as e:
        logger.error(f"压缩过程中出错: {e}，尝试简单摘要", exc_info=True)
        return compress_messages(messages, llm=None)

