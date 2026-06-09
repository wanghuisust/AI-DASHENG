"""Agent 核心：Agent 图定义

架构参考 Hermes Agent，用 Agent 图结构实现：

    ┌─────────┐     ┌──────────┐     ┌──────────┐
    │  用户输入 │────▶│  LLM 思考  │────▶│  工具执行  │
    └─────────┘     └──────────┘     └──────────┘
                         │                │
                         │   (有tool_call) │
                         │◀───────────────┘
                         │
                    (无tool_call)
                         │
                         ▼
                    ┌──────────┐
                    │  返回用户  │
                    └──────────┘
"""

import json
import os
import threading
from typing import Annotated, Literal
from typing_extensions import TypedDict
from langgraph.graph import StateGraph, END
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage

from tools import ALL_TOOLS
from memory import Memory
from skills import SkillManager
from context_compress import compress_messages
from constants import estimate_tokens, get_max_context_tokens, trim_messages_to_tokens

# ── State 定义 ──────────────────────────────────────────────

class AgentState(TypedDict):
    """Agent 状态，贯穿整个图执行"""
    messages: Annotated[list, add_messages]


# ── LLM 初始化 ─────────────────────────────────────────────

def create_llm(model: str = None, base_url: str = None, api_key: str = None):
    """创建绑定工具的 LLM 实例"""
    import os
    from dotenv import load_dotenv
    from pathlib import Path
    env_path = Path(__file__).resolve().parent.parent / ".env"
    load_dotenv(env_path, override=True)

    _model = model or os.getenv("MODEL_NAME", "gpt-4o-mini")
    _base_url = base_url or os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    _api_key = api_key or os.getenv("OPENAI_API_KEY")
    print(f"[LLM-INIT] model={_model} base_url={_base_url} api_key={(_api_key or '')[:8]}...", flush=True)

    llm = ChatOpenAI(
        model=_model,
        base_url=_base_url,
        api_key=_api_key,
        temperature=0.3,
        # 单次 LLM 调用超时 300 秒（与 Hermes 的 1800s 对齐，小模型适当缩短）
        request_timeout=300,
        # 遇到 429 rate limit 自动重试，最多 5 次，指数退避
        max_retries=5,
        # streaming=True：允许 astream_events v2 获取逐 token 事件
        # 同时 agent_node 内部用 llm.stream() 逐 chunk 收集+合并
        streaming=True,
    )
    if os.getenv("ENABLE_TOOLS", "true").lower() == "true":
        return llm.bind_tools(ALL_TOOLS)
    return llm


# ── 图节点 ──────────────────────────────────────────────────
#
# 三层 System Prompt 架构（对标 Hermes Agent）
#
# | 层级   | 内容                                 | 变化频率 |
# |--------|--------------------------------------|----------|
# | stable | 身份 + 工具说明 + 工作原则           | 几乎不变 |
# | context| 匹配的技能（skill）上下文             | 每轮可能变 |
# | volatile| 当前日期/时间                       | 每次请求变 |
#
# 好处：stable 部分字节稳定，有利于 API provider 的 prefix cache 命中，
# 减少 token 消耗。Hermes 的做法：system prompt 构建一次后缓存，
# 只在压缩时重建。

_STABLE_PROMPT = """你是 DASHENG AI，一个本地 AI Agent，由自由AI爱好者H开发。

你的身份：
- 你是 DASHENG AI，不是任何外部模型的名称
- 当被问"你是谁"时，回答：我是 DASHENG AI，由自由AI爱好者H开发，我可以帮助你操控电脑完成任务

你的核心能力：
1. 执行终端命令（terminal_execute）
2. 读写文件（read_file, write_file）
3. 搜索文件（search_files）— 支持两种模式：
   - target="files"：按文件名搜索（如 *.py, *config*）
   - target="content"：按文件内容搜索（如 train_run, loss=），替代 grep/findstr
   - 还支持分页（offset/limit）、输出模式（files_only/count）、上下文行（context）
4. 网络搜索（web_search）
5. 持久记忆（memory_save, memory_search, memory_forget）— 你可以主动保存需要跨会话保留的信息
6. 技能管理（skill_view, skill_install, skill_list, skill_search, skill_remove）— 查看技能详情、按需安装专业工作流
7. 临时文件管理（cleanup_tmp_files）— 任务完成后清理临时脚本

══════════════════════════════════════════════
工作原则（参考 Hermes Agent 行为约束）
══════════════════════════════════════════════

## 基本原则
- 直接执行，不要反复确认
- 遇到错误时自动修复重试
- 用中文回复用户
- 回复简洁，不要啰嗦
- 当你发现值得记住的信息时（用户偏好、环境配置、经验教训），主动用 memory_save 保存
- 当遇到似曾相识的问题时，用 memory_search 查看是否有相关记忆
- **临时文件管理**：写入 .py/.sh/.bat 等临时脚本文件时，write_file 会自动重定向到临时缓冲目录，任务完成后调用 cleanup_tmp_files 清理

## 技能优先原则（关键！必须遵守！）
- system prompt 中已注入所有技能的**名称+描述索引**（见上方"可用技能"列表）
- **第一步**：收到任务后，**立即扫描技能索引**，判断是否有匹配当前任务的技能
- 匹配条件：技能名称或描述中包含任务相关的关键词（如"拉取源码"→github-repo-management、"调试"→python-debugger）
- **如果匹配到技能，必须先调用 skill_view(name) 加载技能详情，再按技能步骤执行**
- **禁止**：不加载技能就直接用 terminal_execute 或 web_search 裸执行——你会错过关键步骤（如代理配置、认证方式）
- 加载技能后，**严格按技能步骤执行**，不要跳步或自创方案
- 如果没有匹配到技能但任务复杂，主动用 skill_search 搜索 ClawHub 是否有可用技能
- 找到适合的技能时，用 skill_install 安装，然后用 skill_view 加载详情再按步骤执行
- 用户要求安装技能时，用 skill_install(source="技能名") 从 ClawHub 安装，或 skill_install(source="GitHub URL") 从 GitHub 安装

## 工具使用约束（关键！）

### 1. 文件路径必须由工具验证
- 涉及文件路径的回答，**必须**先调用 search_files 或 read_file 验证文件存在
- **禁止**凭记忆、凭推测给出文件路径——你以为的路径大概率是错的
- 如果工具搜索未找到，如实告知用户，不要编造路径

### 2. 搜索文件用 search_files，不要用终端命令
- 搜索文件时，**必须使用 search_files 工具**，它基于 ripgrep，快速且精准
- **绝对禁止**用 terminal_execute 跑 `where /r`、`dir /s`、`find`、`Get-ChildItem -Recurse` 等全盘扫描命令——这些在 G 盘等大分区会超时，且浪费大量 token
- **绝对禁止**用 terminal_execute 跑 `grep`、`ripgrep`、`find`、`findstr` 搜索文件内容——用 search_files 替代
- 违反此规则会导致重复调用和超时，严重影响用户体验

### 3. 查找/搜索类请求必须调用工具
- 用户要求"查找/搜索/找/有没有"时，**必须至少调用1次工具**，不允许纯文本敷衍
- 禁止回复"我来帮你找"但实际不调用任何工具
- 禁止回复"我再仔细找找"但不执行搜索
- 如果搜索无结果，如实告知并建议用户换个关键词，不要凭空编造结果

### 4. 优先用工具验证事实
- 不仅在被质疑时，**首次提问也需要验证**——凡是涉及具体文件、路径、配置、状态的回答，先调工具确认再回答
- 不要只回复文字猜测，要用工具输出作为回答依据
- 如果工具结果与你的记忆矛盾，以工具结果为准

### 5. 避免不必要的工具调用
- 如果用户只是闲聊或问常识性问题，直接回答即可，不需要调用工具
- 不需要对每个问题都调工具——常识、定义、翻译等直接回答
- 判断标准：**答案是否取决于当前系统/文件/网络状态**？如果是→调工具；否→直接回答

### 6. web_search 关键词用中文
- 搜索中文内容（国内新闻、中文技术资料等）时，**必须用中文关键词**，不要用英文翻译
- 例：搜 GLM 最新版本 → 用 "GLM 最新模型版本"，不要用 "GLM model latest version"
- 英文关键词对中文搜索引擎效果差，返回结果不相关

### 7. 工具失败时如实报告
- 工具调用失败（超时、报错、无结果）时，如实告知用户失败原因
- **禁止**在工具失败后凭记忆补充"可能的路径"或"大概的位置"
- 可以建议用户换关键词、换路径、或手动检查

## 回复风格

- **直接给结果**，不要先说"好的"或"让我来帮你"
- **不要重复用户的问题**
- 如果调了多个工具，在最终回复里汇总结果，不要把中间过程全贴出来
- 代码/命令只给关键部分，不要大段贴工具原始输出
- 出错时说清楚哪里错了、怎么修，不要只说"出错了"
"""

# 兼容旧代码引用
SYSTEM_PROMPT = _STABLE_PROMPT

# 模块级单例
_memory = Memory()
_skill_manager = SkillManager(os.path.join(os.path.dirname(__file__), "..", "data", "skills"))


def _get_compress_llm(llm):
    """从绑定了工具的 LLM 实例中提取一个不带工具的轻量实例，用于上下文摘要

    绑定了工具的 LLM 在摘要时会尝试调用工具而非直接回答，
    这会导致摘要失败（摘要需要纯文本输出）。
    解法：用相同配置创建一个新的 ChatOpenAI，不带工具绑定。
    """
    try:
        # 尝试从 bound llm 中提取底层配置
        if hasattr(llm, 'bound') and hasattr(llm.bound, 'model'):
            # 这是 .bind_tools() 后的 RunnableBinding，底层是 ChatOpenAI
            base = llm.bound
            from langchain_openai import ChatOpenAI
            return ChatOpenAI(
                model=base.model,
                base_url=base.openai_api_base if hasattr(base, 'openai_api_base') else None,
                api_key=base.openai_api_key if hasattr(base, 'openai_api_key') else None,
                temperature=0.1,
                request_timeout=30,  # 摘要不需要太长超时
                max_retries=2,
            )
        # 直接传入的就是 ChatOpenAI（未绑定工具的情况）
        if hasattr(llm, 'model'):
            from langchain_openai import ChatOpenAI
            return ChatOpenAI(
                model=llm.model,
                base_url=llm.openai_api_base if hasattr(llm, 'openai_api_base') else None,
                api_key=llm.openai_api_key if hasattr(llm, 'openai_api_key') else None,
                temperature=0.1,
                request_timeout=30,
                max_retries=2,
            )
    except Exception as e:
        print(f"[COMPRESS] 无法创建摘要 LLM: {str(e)[:200]}，将使用简单摘要", flush=True)
    return None  # 回退到简单摘要


def _ensure_message_role_continuity(messages: list) -> list:
    """确保消息角色连续性：每个 ToolMessage 前面必须有配对的 AIMessage（带 tool_calls）。
    
    根因：trim_messages_to_tokens 从后往前截断，可能截掉 AI tool_calls 消息但保留其后的 ToolMessage；
    compress_messages 把旧消息压成摘要，AI tool_calls 结构信息丢失，ToolMessage 变成孤立。
    这导致 API 报 400: "No user query found in messages"。
    """
    if not messages:
        return messages
    
    # 收集所有 AI 消息的 tool_call_id
    ai_tool_ids = set()
    for msg in messages:
        if hasattr(msg, 'type') and msg.type == 'ai' and hasattr(msg, 'tool_calls') and msg.tool_calls:
            for tc in msg.tool_calls:
                tc_id = tc.get('id', '')
                if tc_id:
                    ai_tool_ids.add(tc_id)
    
    # 从后往前扫描，跳过没有配对 AI 的 ToolMessage
    cleaned = []
    skipped = 0
    for msg in messages:
        if hasattr(msg, 'type') and msg.type == 'tool':
            tc_id = getattr(msg, 'tool_call_id', '')
            if tc_id and tc_id not in ai_tool_ids:
                skipped += 1
                print(f"[MSG-FIX] Skipping orphan ToolMessage: tool_call_id={tc_id}, name={getattr(msg, 'name', '?')}", flush=True)
                continue
        cleaned.append(msg)
    
    if skipped:
        print(f"[MSG-FIX] Removed {skipped} orphan ToolMessage(s), {len(messages)} → {len(cleaned)} messages", flush=True)
    
    # 确保不以 ToolMessage 开头（第一条非 system 消息必须是 human）
    while cleaned and hasattr(cleaned[0], 'type') and cleaned[0].type == 'tool':
        print(f"[MSG-FIX] Removing leading ToolMessage: {getattr(cleaned[0], 'name', '?')}", flush=True)
        cleaned.pop(0)
    
    # 确保消息序列中有 human 消息（API 要求至少一条 user 消息）
    has_human = any(hasattr(m, 'type') and m.type == 'human' for m in cleaned)
    if not has_human and len(cleaned) >= 2:
        # 找到第一条 system 之后的 system 消息（通常是摘要），改为 human
        for i in range(1, len(cleaned)):
            if hasattr(cleaned[i], 'type') and cleaned[i].type == 'system':
                old_content = cleaned[i].content
                # 将摘要 system 消息转为 human 消息
                from langchain_core.messages import HumanMessage
                cleaned[i] = HumanMessage(content=old_content)
                print(f"[MSG-FIX] No human message found, converted summary system→human (idx={i})", flush=True)
                break
        else:
            # 没有摘要 system，在第一条 system 后插入 dummy human
            from langchain_core.messages import HumanMessage
            cleaned.insert(1, HumanMessage(content="继续"))
            print(f"[MSG-FIX] No human message found, inserted dummy '继续' at idx=1", flush=True)
    
    return cleaned


def _detect_stuck_loop(messages: list) -> list:
    """检测并清理 AI 死循环：连续多条 AI 回复都说'无法连接/失败'但没有 tool_calls
    保留第一条和最后一条，中间的替换为一条摘要
    """
    STUCK_KEYWORDS = ["无法连接", "暂时无法", "连接失败", "搜索失败", "网络异常", "无法访问"]
    
    # 找出连续的"卡住"AI消息（无tool_calls + 包含失败关键词）
    stuck_ranges = []  # [(start_idx, end_idx), ...]
    i = 0
    while i < len(messages):
        msg = messages[i]
        if (hasattr(msg, 'type') and msg.type == 'ai' 
            and not (hasattr(msg, 'tool_calls') and msg.tool_calls)
            and msg.content):
            content = msg.content if isinstance(msg.content, str) else str(msg.content)
            if any(kw in content for kw in STUCK_KEYWORDS):
                start = i
                while i < len(messages):
                    m = messages[i]
                    if (hasattr(m, 'type') and m.type == 'ai' 
                        and not (hasattr(m, 'tool_calls') and m.tool_calls)
                        and m.content):
                        c = m.content if isinstance(m.content, str) else str(m.content)
                        if any(kw in c for kw in STUCK_KEYWORDS):
                            i += 1
                            continue
                    break
                end = i  # exclusive
                if end - start >= 2:
                    stuck_ranges.append((start, end))
                continue
        i += 1
    
    if not stuck_ranges:
        return messages
    
    # 合并每个stuck范围：保留第一条，删掉中间的，保留最后一条
    result = list(messages)
    for start, end in reversed(stuck_ranges):
        if end - start > 2:
            # 保留第一条和最后一条，中间替换为一条摘要
            from langchain_core.messages import AIMessage
            summary_msg = AIMessage(content="[之前多次尝试搜索均失败，已省略中间重复记录]")
            result[start+1:end-1] = [summary_msg]
    
    print(f"[LOOP-CLEAN] 检测到 {len(stuck_ranges)} 处死循环，已清理", flush=True)
    return result


def agent_node(state: AgentState, llm, cancel_event: threading.Event = None) -> dict:
    """LLM 思考节点：决定下一步是调用工具还是回复用户
    cancel_event: 外部传入的取消信号，如果被 set 则立即返回
    """
    messages = state["messages"]

    # 0. 检查是否已被取消
    if cancel_event and cancel_event.is_set():
        return {"messages": [AIMessage(content="(请求已取消)")]}

    # 0.5 清理死循环历史（连续多条"搜索失败"的AI消息）
    messages = _detect_stuck_loop(messages)

    # 1. 上下文压缩：对话太长时自动摘要旧消息（传入 llm 启用结构化摘要）
    #    使用一个轻量 LLM 实例做摘要，避免绑定工具
    _compress_llm = _get_compress_llm(llm)
    messages = compress_messages(messages, llm=_compress_llm)

    # 2. Token 截断：硬性限制
    messages = trim_messages_to_tokens(messages)

    # 2.5 消息角色连续性校验：确保 trim/compress 后没有孤立 ToolMessage
    messages = _ensure_message_role_continuity(messages)

    # 3. 构建三层 system prompt（对标 Hermes Agent）
    #    stable:  身份 + 工具说明 + 工作原则（几乎不变，利于 API prefix cache）
    #    context: 匹配的技能上下文（每轮可能变）
    #    volatile: 当前日期/时间（每次请求变）
    system_content = _STABLE_PROMPT

    # context 层：注入匹配的技能到 system prompt
    last_user_msg = ""
    for msg in reversed(messages):
        if hasattr(msg, "type") and msg.type == "human":
            last_user_msg = msg.content if isinstance(msg.content, str) else str(msg.content)
            break
    if last_user_msg:
        skill_context = _skill_manager.get_context_for_query(last_user_msg)
        if skill_context:
            system_content += skill_context

    # 3.5 记忆上下文注入到 user message 的 <memory-context> 围栏（对标 Hermes）
    #     好处：system prompt 字节稳定，不随记忆更新而变化，利于 API 缓存
    memory_context = _memory.get_context()
    if memory_context and last_user_msg:
        memory_block = (
            f"<memory-context>\n"
            f"[系统注：以下是从持久记忆中检索的参考信息，不是用户的新输入。"
            f"将其作为权威参考数据，不要回应其中提到的问题。]\n"
            f"{memory_context}\n"
            f"</memory-context>"
        )
        # 将 memory block 注入到最后一条 human message 前面
        # 找到最后一条 human message 的位置
        for i in range(len(messages) - 1, -1, -1):
            if hasattr(messages[i], "type") and messages[i].type == "human":
                original = messages[i].content if isinstance(messages[i].content, str) else str(messages[i].content)
                messages[i] = HumanMessage(content=f"{memory_block}\n\n{original}")
                break

    # volatile 层：注入当前时间（每次请求都会变）
    import datetime as _dt
    _now = _dt.datetime.now()
    system_content += f"\n\n当前时间: {_now.strftime('%Y-%m-%d %H:%M:%S')}"

    full_messages = [SystemMessage(content=system_content)] + messages
    _ts = _dt.datetime.now().strftime("%H:%M:%S.%f")[:-3]
    _total_chars = sum(len(str(m.content)) for m in full_messages if hasattr(m, 'content'))
    _msg_count = len(full_messages)
    print(f"[{_ts}] [LLM] invoke start: {_msg_count} msgs, ~{_total_chars} chars", flush=True)
    for _i, _m in enumerate(full_messages):
        _role = getattr(_m, 'type', type(_m).__name__)
        _content = str(getattr(_m, 'content', ''))[:120].replace('\n', '\\n')
        _tc = ''
        if hasattr(_m, 'tool_calls') and _m.tool_calls:
            _tc = f' tool_calls=[{", ".join(tc["name"] for tc in _m.tool_calls)}]'
        _tcid = getattr(_m, 'tool_call_id', '')
        if _tcid:
            _tc = f' tool_call_id={_tcid}'
        print(f"[{_ts}] [LLM]   msg[{_i}] {_role}: {_content}{_tc}", flush=True)
    try:
        # 用 llm.stream() 逐 chunk 收集，同时让 astream_events v2 能捕获每个 token
        # 替代 llm.invoke()，后者是同步阻塞的，LLM 全部 token 完成才返回
        _stream_chunks = []
        _stream_interrupted = False

        for chunk in llm.stream(full_messages):
            if cancel_event and cancel_event.is_set():
                print(f"[{_ts}] [LLM] stream CANCELLED by cancel_event", flush=True)
                _stream_interrupted = True
                break
            _stream_chunks.append(chunk)

        if _stream_interrupted:
            return {"messages": [AIMessage(content="(请求已取消)")]}

        # 合并所有 chunks 为完整 AIMessage
        if _stream_chunks:
            response = _stream_chunks[0]
            for c in _stream_chunks[1:]:
                response = response + c
        else:
            response = AIMessage(content="")

        _resp_chars = len(str(response.content)) if response.content else 0
        _resp_tc = ''
        if hasattr(response, 'tool_calls') and response.tool_calls:
            _resp_tc = f' tool_calls=[{", ".join(tc["name"] for tc in response.tool_calls)}]'
        print(f"[{_ts}] [LLM] stream OK: {_resp_chars} chars{_resp_tc}", flush=True)
        # ── 空回复处理（与 Hermes 对齐：信任 LLM 判断）──
        # Hermes 的做法：LLM 返回纯文本 → 直接返回，没有"空意图重试"。
        # 小模型可能"只说不做"，但强制重试往往让情况更糟。
        # 只在 LLM 完全没输出（空字符串）时才重试一次。
        if not (hasattr(response, 'tool_calls') and response.tool_calls) and response.content:
            _content = response.content if isinstance(response.content, str) else str(response.content)
            
            if not _content.strip():
                print(f"[{_ts}] [EMPTY-INTENT] LLM 返回空内容，重试一次", flush=True)
                retry_messages = full_messages + [response, HumanMessage(
                    content="[系统提示：你刚才没有给出任何回复，请重新回答用户的问题。如果需要查找信息，请直接调用工具。]"
                )]
                _retry_chunks = []
                _retry_error = [None]
                try:
                    for chunk in llm.stream(retry_messages):
                        if cancel_event and cancel_event.is_set():
                            return {"messages": [AIMessage(content="(请求已取消)")]}
                        _retry_chunks.append(chunk)
                    if _retry_chunks:
                        response = _retry_chunks[0]
                        for c in _retry_chunks[1:]:
                            response = response + c
                        print(f"[{_ts}] [EMPTY-INTENT] 重试成功: {len(str(response.content or ''))} chars", flush=True)
                except Exception as se:
                    print(f"[{_ts}] [EMPTY-INTENT] 重试失败: {str(se)[:200]}", flush=True)
    except Exception as e:
        import traceback as _tb
        _error_full = str(e)
        print(f"[{_ts}] [LLM] invoke FAILED: {_error_full[:1000]}", flush=True)
        _tb.print_exc()
        # LLM 调用失败（如 401 认证错误），直接返回错误消息，不再循环重试
        if "401" in _error_full or "Authentication" in _error_full:
            return {"messages": [AIMessage(content="抱歉，AI 服务暂时认证失败，请稍后重试。")]}
        if "429" in _error_full or "rate" in _error_full.lower():
            return {"messages": [AIMessage(content="抱歉，AI 服务请求过于频繁，请稍后重试。")]}
        if "400" in _error_full:
            # 400 通常是请求格式问题，打印完整错误帮助诊断
            print(f"[{_ts}] [LLM] 400 FULL ERROR: {_error_full}", flush=True)
        return {"messages": [AIMessage(content=f"抱歉，AI 服务调用出错，请稍后重试。错误：{_error_full[:200]}")]}
    return {"messages": [response]}


def should_continue(state: AgentState) -> Literal["tools", "end"]:
    """条件边：判断 LLM 是否要调用工具，同时检测死循环
    
    参考 Hermes Agent 的 ToolCallGuardrailController，实现三层循环检测：
    1. 精确重复（exact_failure）：同工具+同参数反复调用
    2. 同类重复（same_tool）：同工具名反复调用（不同参数）
    3. 交替循环（alternating_loop）：两个工具交替调用
    """
    last_message = state["messages"][-1]
    if not (hasattr(last_message, "tool_calls") and last_message.tool_calls):
        return "end"

    # ── 收集最近的工具调用+结果历史 ──
    # 格式：[(tool_name, args_hash, result_hash), ...]，按时间正序
    import hashlib
    import json as _json

    recent_calls = []  # [(tool_name, args_hash), ...]
    recent_results = []  # [result_hash, ...] 对应每次调用的结果摘要
    # 同时收集 tool 结果消息，用于判断是否在进步
    _tool_results = []  # [(tool_call_id, result_hash), ...]
    for msg in state["messages"]:
        if hasattr(msg, "type") and msg.type == "tool":
            _content = msg.content if isinstance(msg.content, str) else str(msg.content)
            _tcid = getattr(msg, "tool_call_id", "")
            _tool_results.append((_tcid, hashlib.md5(_content[:500].encode()).hexdigest()[:8]))

    for msg in reversed(state["messages"]):
        if hasattr(msg, "type") and msg.type == "ai" and hasattr(msg, "tool_calls") and msg.tool_calls:
            for tc in msg.tool_calls:
                try:
                    args_str = _json.dumps(tc["args"], sort_keys=True, ensure_ascii=False, default=str)
                except (TypeError, ValueError):
                    args_str = str(tc.get("args", {}))
                args_hash = hashlib.md5(args_str.encode()).hexdigest()[:8]
                recent_calls.append((tc["name"], args_hash))
                # 找对应的结果 hash
                _tcid = tc.get("id", "")
                _res_hash = ""
                for _tid, _rh in _tool_results:
                    if _tid == _tcid:
                        _res_hash = _rh
                        break
                recent_results.append(_res_hash)
        elif hasattr(msg, "type") and msg.type == "tool":
            continue  # 跳过工具结果消息
        else:
            break  # 遇到非工具链消息就停
    recent_calls.reverse()
    recent_results.reverse()

    if not recent_calls:
        return "tools"

    import datetime as _dt
    _ts = _dt.datetime.now().strftime("%H:%M:%S")

    # ── 辅助：判断同工具名的最近几次调用是否在"进步" ──
    # 进步 = 最近的结果不全相同（说明每次返回了不同信息，正在探索）
    def _is_making_progress(tool_name: str, min_calls: int = 3) -> bool:
        """检查该工具最近 min_calls 次调用的结果是否互不相同（在进步）"""
        _results = [recent_results[i] for i, (tn, _) in enumerate(recent_calls) if tn == tool_name]
        if len(_results) < min_calls:
            return False  # 次数不够，无法判断
        _last_n = _results[-min_calls:]
        # 如果结果 hash 不全相同，说明在产出新信息
        unique = set(_last_n)
        if len(unique) > 1:
            return True  # 结果在变化 → 在探索
        return False  # 结果都一样 → 在死循环

    # ── 检测1：精确重复 — 同工具+同参数出现 3 次以上 ──
    # 改进：同参数≠死循环，如果返回了不同结果说明环境在变，放行
    EXACT_REPEAT_LIMIT = 3        # 同参数调3次以上才考虑
    EXACT_REPEAT_HARD_LIMIT = 5   # 同参数硬上限

    call_signatures = {}  # (tool_name, args_hash) → count
    for tool_name, args_hash in recent_calls:
        sig = (tool_name, args_hash)
        call_signatures[sig] = call_signatures.get(sig, 0) + 1

    for (tool_name, args_hash), count in call_signatures.items():
        if count >= EXACT_REPEAT_HARD_LIMIT:
            # 硬上限，无论如何杀
            print(f"[{_ts}] [LOOP-DETECT] 精确重复硬上限: {tool_name}(args_hash={args_hash}) 被调用 {count} 次，强制结束", flush=True)
            last_message.tool_calls = []
            if not last_message.content:
                last_message.content = f"抱歉，{tool_name} 被重复调用了太多次（参数相同），已自动停止。请换一种方式提问。"
            return "end"
        if count >= EXACT_REPEAT_LIMIT:
            # 检查同参数调用的结果是否在变化
            _sig_results = []
            for i, (tn, ah) in enumerate(recent_calls):
                if tn == tool_name and ah == args_hash:
                    _sig_results.append(recent_results[i])
            _unique_results = set(_sig_results)
            if len(_unique_results) <= 1 or "" in _sig_results:
                # 结果都一样（或还没拿到结果）→ 真死循环
                print(f"[{_ts}] [LOOP-DETECT] 精确重复(无进步): {tool_name}(args_hash={args_hash}) 被调用 {count} 次，结果相同，强制结束", flush=True)
                last_message.tool_calls = []
                if not last_message.content:
                    last_message.content = f"抱歉，{tool_name} 被重复调用了太多次（参数相同，结果相同），已自动停止。请换一种方式提问。"
                return "end"
            else:
                # 同参数但返回了不同结果 → 环境/状态在变，不是死循环
                print(f"[{_ts}] [LOOP-DETECT] 精确重复但结果在变化: {tool_name}(args_hash={args_hash}) {count}次，放行", flush=True)

    # ── 检测2：同类重复 — 同工具名出现多次 ──
    # 关键改进：如果每次返回不同结果（在进步/探索），只打警告不杀；只有真正死循环才杀
    SAME_TOOL_LIMIT = 8        # 同工具调 8 次以上才考虑（给足探索空间）
    SAME_TOOL_HARD_LIMIT = 30  # 硬上限，与 Hermes max_iterations 对齐

    tool_name_counts = {}
    for tool_name, _ in recent_calls:
        tool_name_counts[tool_name] = tool_name_counts.get(tool_name, 0) + 1

    for tool_name, count in tool_name_counts.items():
        if count >= SAME_TOOL_HARD_LIMIT:
            print(f"[{_ts}] [LOOP-DETECT] 硬上限: {tool_name} 被调用 {count} 次（上限{SAME_TOOL_HARD_LIMIT}），强制结束", flush=True)
            last_message.tool_calls = []
            if not last_message.content:
                last_message.content = f"抱歉，{tool_name} 调用次数已达上限（{count}次），已自动停止。"
            return "end"
        if count >= SAME_TOOL_LIMIT:
            if _is_making_progress(tool_name):
                # 在进步 → 不杀，也不注入畸形消息（与 Hermes 对齐：信任 LLM 自己收敛）
                print(f"[{_ts}] [LOOP-DETECT] 同工具 {tool_name} 调用 {count} 次但在进步，放行", flush=True)
                # 不修改 last_message.content——在带 tool_calls 的 AIMessage 里塞文本
                # 会导致 LLM 收到畸形消息，行为不可预测
                return "tools"
            else:
                # 没进步（结果都一样）→ 真死循环，杀
                print(f"[{_ts}] [LOOP-DETECT] 同类重复(无进步): {tool_name} 被调用 {count} 次，结果均相同，强制结束", flush=True)
                last_message.tool_calls = []
                if not last_message.content:
                    last_message.content = f"抱歉，{tool_name} 被反复调用了 {count} 次（结果均相同），已自动停止。请换一种方式提问。"
                return "end"

    # ── 检测3：交替循环 — 最近 8 次调用只涉及 2 个工具名交替 ──
    # 同样加入进步检测：如果交替调用产出了不同结果，不算死循环
    if len(recent_calls) >= 10:
        last_8_names = [name for name, _ in recent_calls[-10:]]
        unique_names = set(last_8_names)
        if len(unique_names) <= 2:
            # 检查最近几次的结果是否在变化
            _last_results = recent_results[-6:]
            if len(set(_last_results)) <= 2:
                # 结果也在重复 → 真循环
                print(f"[{_ts}] [LOOP-DETECT] 交替循环(无进步): 最近10次调用仅涉及 {unique_names}，结果重复，强制结束", flush=True)
                last_message.tool_calls = []
                if not last_message.content:
                    last_message.content = "抱歉，检测到工具调用陷入循环，已自动停止。请换一种方式提问或稍后重试。"
                return "end"
            else:
                # 交替但产出不同结果 → 在探索
                print(f"[{_ts}] [LOOP-DETECT] 交替调用但结果在变化，放行", flush=True)

    return "tools"


# ── 构建图 ──────────────────────────────────────────────────

def build_graph(llm=None, cancel_event: threading.Event = None):
    """构建 Agent 图
    cancel_event: 传入取消信号，LLM invoke 等待期间可被中断
    """
    if llm is None:
        llm = create_llm()

    tool_node = ToolNode(ALL_TOOLS)

    graph = StateGraph(AgentState)
    graph.add_node("agent", lambda state: agent_node(state, llm, cancel_event))
    graph.add_node("tools", tool_node)
    graph.set_entry_point("agent")
    graph.add_conditional_edges("agent", should_continue, {"tools": "tools", "end": END})
    graph.add_edge("tools", "agent")

    return graph.compile()


# ── 便捷函数 ────────────────────────────────────────────────

def chat(user_input: str, messages: list = None, graph=None) -> str:
    """单轮对话便捷函数"""
    if graph is None:
        graph = build_graph()
    if messages is None:
        messages = []

    messages.append(HumanMessage(content=user_input))
    result = graph.invoke({"messages": messages})

    last_ai = None
    for msg in result["messages"]:
        if hasattr(msg, "content") and msg.type == "ai" and msg.content:
            last_ai = msg

    return last_ai.content if last_ai else "(无回复)"
