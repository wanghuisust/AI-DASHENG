"""上下文压缩 — 对话过长时自动摘要，节省 token（借鉴 Hermes Agent 方案）

策略（对标 Hermes ContextCompressor）：
  Phase 1: 工具输出预处理 — 去重、截断冗长的工具返回
  Phase 2: 边界计算 — 基于 token 预算，而非固定消息条数
  Phase 3: LLM 结构化摘要 — 增量更新，保留关键上下文
  Phase 4: 回退 — LLM 不可用时简单摘要兜底

与旧版区别：
  - 旧版：固定保留最近 6 条，不区分工具输出，简单截断
  - 新版：基于 token 预算动态分割，工具输出先瘦身，LLM 生成结构化摘要
"""

import hashlib
import json as _json
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage, ToolMessage
from constants import estimate_tokens, get_compress_threshold, get_max_context_tokens

# ── 可调参数 ──
TAIL_TOKEN_RATIO = 0.30    # 尾部保留区占总预算的比例（Hermes: ~30%）
MIN_TAIL_MESSAGES = 4      # 尾部最少保留的消息条数
SUMMARY_MAX_CHARS = 3000   # LLM 摘要最大字符数
TOOL_OUTPUT_MAX_CHARS = 500  # 工具输出预处理后最大字符数


# ═══════════════════════════════════════════════════════════
# Phase 1: 工具输出预处理
# ═══════════════════════════════════════════════════════════

def _content_hash(text: str) -> str:
    """短哈希，用于工具输出去重"""
    return hashlib.md5(text[:1000].encode()).hexdigest()[:8]


def _summarize_tool_output(name: str, content: str) -> str:
    """将冗长的工具输出压缩为一行摘要"""
    if not content:
        return content
    # 如果已经够短，直接返回
    if len(content) <= TOOL_OUTPUT_MAX_CHARS:
        return content

    # 工具名到摘要策略的映射
    name_lower = (name or "").lower()

    # 搜索/列表类：只保留前几行 + 行数统计
    if any(kw in name_lower for kw in ["search", "find", "list", "glob", "ls"]):
        lines = content.strip().split("\n")
        kept = "\n".join(lines[:8])
        return f"{kept}\n... (共 {len(lines)} 行，已省略)"

    # 读取文件类：只保留前几行
    if any(kw in name_lower for kw in ["read", "cat", "head"]):
        lines = content.strip().split("\n")
        kept = "\n".join(lines[:10])
        return f"{kept}\n... (共 {len(lines)} 行，已省略)"

    # 终端命令类：保留最后几行（通常包含结果）
    if any(kw in name_lower for kw in ["terminal", "shell", "exec", "bash"]):
        lines = content.strip().split("\n")
        if len(lines) <= 15:
            return content
        kept = "\n".join(lines[:3] + ["..."] + lines[-5:])
        return f"{kept}\n... (共 {len(lines)} 行，已省略)"

    # 通用：截断到最大长度
    return content[:TOOL_OUTPUT_MAX_CHARS] + "\n... (已截断)"


def _prune_tool_outputs(messages: list) -> list:
    """预处理工具输出：去重 + 截断冗长返回

    类似 Hermes 的 _prune_old_tool_outputs，但简化为：
    1. 同名工具相同输出的重复调用只保留第一次
    2. 超长工具输出截断
    """
    seen_tool_hashes = {}  # (tool_name, output_hash) → first_index
    result = []

    for i, msg in enumerate(messages):
        if not (hasattr(msg, "type") and msg.type == "tool"):
            result.append(msg)
            continue

        tool_name = getattr(msg, "name", "tool")
        content = msg.content if isinstance(msg.content, str) else str(msg.content or "")
        output_hash = _content_hash(content)

        key = (tool_name, output_hash)
        if key in seen_tool_hashes:
            # 重复的工具输出，替换为简短引用
            first_idx = seen_tool_hashes[key]
            new_msg = ToolMessage(
                content=f"(同第 {first_idx + 1} 条消息的 {tool_name} 输出，已去重)",
                tool_call_id=getattr(msg, "tool_call_id", ""),
                name=tool_name,
            )
            result.append(new_msg)
        else:
            seen_tool_hashes[key] = i
            # 截断超长输出
            trimmed = _summarize_tool_output(tool_name, content)
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
# Phase 2: 边界计算 — 基于 token 预算
# ═══════════════════════════════════════════════════════════

def _find_tail_boundary(messages: list, budget: int) -> int:
    """从消息列表末尾往前扫描，找到 token 预算内的分割点

    Returns:
        分割索引：messages[:split] 是旧消息（待摘要），messages[split:] 是尾部保留
    """
    tail_tokens = 0
    split = len(messages)

    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        content = ""
        if hasattr(msg, "content") and msg.content:
            content = msg.content if isinstance(msg.content, str) else str(msg.content)
        if hasattr(msg, "tool_calls") and msg.tool_calls:
            content += _json.dumps(msg.tool_calls, ensure_ascii=False, default=str)
        msg_tokens = estimate_tokens(content) + 10

        if tail_tokens + msg_tokens > budget:
            break
        tail_tokens += msg_tokens
        split = i

    # 确保至少保留 MIN_TAIL_MESSAGES 条
    split = min(split, len(messages) - MIN_TAIL_MESSAGES)
    split = max(split, 0)  # 不能为负

    return split


# ═══════════════════════════════════════════════════════════
# Phase 3: LLM 结构化摘要（增量更新）
# ═══════════════════════════════════════════════════════════

# 结构化摘要模板（对标 Hermes 的 summary template）
SUMMARY_TEMPLATE = """请将以下对话历史压缩为结构化摘要。摘要将用于后续对话的上下文注入。

请按以下格式输出（每个部分简洁，1-3 行）：

## 活跃任务
（用户当前正在做什么）

## 关键结论
（对话中已确认的重要事实或决策）

## 已完成操作
（已执行的关键工具调用和结果，按时间简述）

## 待解决事项
（尚未完成或有争议的问题）

## 关键文件/路径
（对话中涉及的文件路径、URL、配置等）

对话历史：
{history}
"""

# 增量更新模板（已有旧摘要时使用）
INCREMENTAL_TEMPLATE = """你是一个对话摘要助手。下面有一段旧摘要和一段新的对话记录。
请将新信息合并到旧摘要中，更新各部分内容。如果旧摘要中的信息不再相关，可以删除。
保持每个部分简洁（1-3 行），总长度不超过 {max_chars} 字符。

旧摘要：
{old_summary}

新对话记录：
{new_history}

请输出更新后的完整摘要（保持原有的 ## 标题结构）：
"""

# 前缀标记：标识这是压缩摘要，不是当前对话
SUMMARY_PREFIX = "[CONTEXT SUMMARY — 这是之前对话的压缩摘要，不是当前对话内容]"


def _messages_to_text(messages: list) -> str:
    """把消息列表转为可读文本（用于 LLM 摘要输入）"""
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
                    args_str = _json.dumps(tc["args"], ensure_ascii=False, default=str)
                    if len(args_str) > 200:
                        args_str = args_str[:200] + "..."
                    lines.append(f"AI调用工具: {tc['name']}({args_str})")
            if content:
                lines.append(f"AI: {content[:500]}")
        elif role == "tool":
            name = getattr(msg, "name", "tool")
            lines.append(f"工具[{name}]返回: {content[:300]}")
        elif role == "system":
            # 旧摘要直接保留
            lines.append(f"[旧摘要]: {content[:1000]}")
        else:
            lines.append(f"{role}: {content[:200]}")

    return "\n".join(lines)


def _find_existing_summary(messages: list) -> tuple:
    """在消息列表中查找已有的摘要消息

    Returns:
        (summary_index, summary_text) 或 (-1, "")
    """
    for i, msg in enumerate(messages):
        if hasattr(msg, "type") and msg.type == "system":
            content = msg.content if isinstance(msg.content, str) else str(msg.content or "")
            if SUMMARY_PREFIX in content:
                # 提取摘要正文（去掉前缀标记）
                body = content.split(SUMMARY_PREFIX, 1)[-1].strip()
                return i, body
    return -1, ""


def _llm_summarize(old_msgs: list, existing_summary: str, llm) -> str:
    """用 LLM 生成结构化摘要（增量更新模式）

    Args:
        old_msgs: 需要摘要的旧消息列表
        existing_summary: 已有的摘要文本（空字符串表示首次摘要）
        llm: ChatOpenAI 实例

    Returns:
        摘要文本
    """
    history_text = _messages_to_text(old_msgs)

    # 截断超长历史
    if len(history_text) > 12000:
        history_text = history_text[:12000] + "\n...(对话历史已截断)"

    if existing_summary:
        # 增量更新模式
        prompt_text = INCREMENTAL_TEMPLATE.format(
            old_summary=existing_summary,
            new_history=history_text,
            max_chars=SUMMARY_MAX_CHARS,
        )
    else:
        # 首次摘要模式
        prompt_text = SUMMARY_TEMPLATE.format(history=history_text)

    try:
        response = llm.invoke([
            SystemMessage(content="你是一个对话摘要助手。输出简洁的结构化摘要，使用中文。"),
            HumanMessage(content=prompt_text),
        ])
        summary = response.content if isinstance(response.content, str) else str(response.content)

        # 截断过长的摘要
        if len(summary) > SUMMARY_MAX_CHARS:
            summary = summary[:SUMMARY_MAX_CHARS] + "\n...(摘要已截断)"

        return summary
    except Exception as e:
        print(f"[COMPRESS] LLM 摘要失败: {str(e)[:200]}，回退到简单摘要", flush=True)
        return _simple_summarize(old_msgs)


# ═══════════════════════════════════════════════════════════
# Phase 4: 简单摘要回退（无 LLM 时使用）
# ═══════════════════════════════════════════════════════════

def _simple_summarize(messages: list) -> str:
    """简单摘要：只保留用户消息和 AI 回复的关键行（兜底方案）"""
    lines = []
    for msg in messages:
        role = getattr(msg, "type", "unknown")
        content = getattr(msg, "content", "") or ""
        if role == "system" and SUMMARY_PREFIX in content:
            # 保留旧摘要
            body = content.split(SUMMARY_PREFIX, 1)[-1].strip()
            lines.append(f"[旧摘要]: {body[:500]}")
        elif role == "human":
            lines.append(f"用户问: {content[:100]}")
        elif role == "ai" and content and not (hasattr(msg, "tool_calls") and msg.tool_calls):
            lines.append(f"AI答: {content[:100]}")
        elif role == "ai" and hasattr(msg, "tool_calls") and msg.tool_calls:
            tool_names = ", ".join(tc["name"] for tc in msg.tool_calls)
            lines.append(f"AI调用: {tool_names}")
    return "\n".join(lines[-30:])


# ═══════════════════════════════════════════════════════════
# 主入口
# ═══════════════════════════════════════════════════════════

def compress_messages(messages: list, llm=None) -> list:
    """压缩消息列表（对标 Hermes ContextCompressor，简化版）

    流程：
    1. 工具输出预处理（去重+截断）
    2. 检查是否需要压缩（token 超阈值）
    3. 基于 token 预算计算分割点
    4. 对旧消息生成/更新结构化摘要
    5. 返回 [摘要SystemMessage] + 尾部保留消息
    """
    if len(messages) <= MIN_TAIL_MESSAGES:
        return messages

    # Phase 1: 工具输出预处理
    messages = _prune_tool_outputs(messages)

    # 检查是否需要压缩
    total_tokens = 0
    for msg in messages:
        content = ""
        if hasattr(msg, "content") and msg.content:
            content = msg.content if isinstance(msg.content, str) else str(msg.content)
        if hasattr(msg, "tool_calls") and msg.tool_calls:
            content += _json.dumps(msg.tool_calls, ensure_ascii=False, default=str)
        total_tokens += estimate_tokens(content) + 10

    threshold = get_compress_threshold()
    if total_tokens < threshold:
        return messages

    # Phase 2: 计算分割点
    max_tokens = get_max_context_tokens()
    tail_budget = int(max_tokens * TAIL_TOKEN_RATIO)
    split = _find_tail_boundary(messages, tail_budget)

    if split == 0:
        # 全部消息都在预算内，无需压缩
        return messages

    old_msgs = messages[:split]
    tail_msgs = messages[split:]

    # Phase 3: 查找已有摘要 + 生成新摘要
    existing_idx, existing_summary = _find_existing_summary(old_msgs)

    # 从旧消息中移除旧的摘要消息（避免重复）
    if existing_idx >= 0:
        old_msgs_for_summary = old_msgs[:existing_idx] + old_msgs[existing_idx + 1:]
    else:
        old_msgs_for_summary = old_msgs

    if llm is not None:
        summary_text = _llm_summarize(old_msgs_for_summary, existing_summary, llm)
    else:
        # 没有 LLM 时合并已有摘要 + 简单摘要
        simple = _simple_summarize(old_msgs_for_summary)
        if existing_summary:
            summary_text = f"{existing_summary}\n\n--- 新增 ---\n{simple}"
        else:
            summary_text = simple

    # Phase 4: 组装结果
    summary_msg = SystemMessage(content=f"{SUMMARY_PREFIX}\n{summary_text}")
    result = [summary_msg] + tail_msgs

    print(f"[COMPRESS] {len(messages)} msgs → 1 summary + {len(tail_msgs)} tail "
          f"(saved {len(old_msgs)} msgs, {total_tokens}→~{estimate_tokens(summary_text)} tokens in summary)",
          flush=True)

    return result
