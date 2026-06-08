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
        # 单次 LLM 调用超时 60 秒（Agnes 免费版较慢）
        request_timeout=60,
        # 遇到 429 rate limit 自动重试，最多 5 次，指数退避
        max_retries=5,
    )
    if os.getenv("ENABLE_TOOLS", "true").lower() == "true":
        return llm.bind_tools(ALL_TOOLS)
    return llm


# ── 图节点 ──────────────────────────────────────────────────

SYSTEM_PROMPT = """你是 DASHENG AI，一个本地 AI Agent，由自由AI爱好者H开发。

你的身份：
- 你是 DASHENG AI，不是任何外部模型的名称
- 当被问"你是谁"时，回答：我是 DASHENG AI，由自由AI爱好者H开发，我可以帮助你操控电脑完成任务

你的核心能力：
1. 执行终端命令（terminal_execute）
2. 读写文件（read_file, write_file）
3. 搜索文件（search_files）
4. 网络搜索（web_search）
5. 持久记忆（memory_save, memory_search, memory_forget）— 你可以主动保存需要跨会话保留的信息
6. 临时文件管理（cleanup_tmp_files）— 任务完成后清理临时脚本

工作原则：
- 直接执行，不要反复确认
- 遇到错误时自动修复重试
- 用中文回复用户
- 回复简洁，不要啰嗦
- 当你发现值得记住的信息时（用户偏好、环境配置、经验教训），主动用 memory_save 保存
- 当遇到似曾相识的问题时，用 memory_search 查看是否有相关记忆
- **优先用工具验证事实**：当用户对某个结果提出质疑或追问时，主动执行相关命令去验证/深挖，不要只回复文字猜测
- **避免不必要的工具调用**：如果用户只是闲聊或问常识性问题，直接回答即可，不需要调用工具
- **临时文件管理**：写入 .py/.sh/.bat 等临时脚本文件时，write_file 会自动重定向到临时缓冲目录，任务完成后调用 cleanup_tmp_files 清理
"""

# 模块级单例
_memory = Memory()
_skill_manager = SkillManager(os.path.join(os.path.dirname(__file__), "..", "data", "skills"))


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

    # 1. 上下文压缩：对话太长时自动摘要旧消息
    messages = compress_messages(messages, llm=None)  # 不用 LLM 摘要，用简单截断（省token）

    # 2. Token 截断：硬性限制
    messages = trim_messages_to_tokens(messages)

    # 3. 构建 system prompt（基础 + 记忆 + 匹配的技能）
    system_content = SYSTEM_PROMPT

    # 注入记忆上下文
    memory_context = _memory.get_context()
    if memory_context:
        system_content += f"\n\n{memory_context}"

    # 注入匹配的技能
    last_user_msg = ""
    for msg in reversed(messages):
        if hasattr(msg, "type") and msg.type == "human":
            last_user_msg = msg.content if isinstance(msg.content, str) else str(msg.content)
            break
    if last_user_msg:
        skill_context = _skill_manager.get_context_for_query(last_user_msg)
        if skill_context:
            system_content += skill_context

    full_messages = [SystemMessage(content=system_content)] + messages
    import datetime as _dt
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
        # 用线程+超时包装 llm.invoke，以便在等待 LLM 响应期间检测 cancel
        _invoke_result = [None]
        _invoke_error = [None]

        def _do_invoke():
            try:
                _invoke_result[0] = llm.invoke(full_messages)
            except Exception as e:
                _invoke_error[0] = e

        _invoke_thread = threading.Thread(target=_do_invoke, daemon=True)
        _invoke_thread.start()

        # 每2秒检查一次 cancel，同时等 invoke 完成
        while _invoke_thread.is_alive():
            if cancel_event and cancel_event.is_set():
                print(f"[{_ts}] [LLM] invoke CANCELLED by cancel_event", flush=True)
                return {"messages": [AIMessage(content="(请求已取消)")]}
            _invoke_thread.join(timeout=2)

        if _invoke_error[0]:
            raise _invoke_error[0]

        response = _invoke_result[0]
        _resp_chars = len(str(response.content)) if response.content else 0
        _resp_tc = ''
        if hasattr(response, 'tool_calls') and response.tool_calls:
            _resp_tc = f' tool_calls=[{", ".join(tc["name"] for tc in response.tool_calls)}]'
        print(f"[{_ts}] [LLM] invoke OK: {_resp_chars} chars{_resp_tc}", flush=True)
        
        # ── 空意图重试：LLM 说要做什么但没生成 tool_calls ──
        if not (hasattr(response, 'tool_calls') and response.tool_calls) and response.content:
            _content = response.content if isinstance(response.content, str) else str(response.content)
            # 空意图判定：短回复(<150字) + 没有实质性结果内容
            # "实质"= LLM 实际给出了数据/列表/路径/代码，而不是复述用户意图
            _has_substance = any(kw in _content for kw in [
                "：\n", ":\n", "```", "1.", "2.", "•", "►",
                "G:\\", "C:\\", "/home", "http://", "https://",
                "详细如下", "列出如下", "找到以下", "结果如下",
            ]) or len(_content) >= 150  # 长回复大概率有实质内容
            _is_intent_only = not _has_substance
            
            if _is_intent_only:
                print(f"[{_ts}] [EMPTY-INTENT] LLM回复太短且无实质内容: '{_content[:60]}'，重试", flush=True)
                # 最多重试3次，逐步加强提示
                _retry_prompts = [
                    "[系统提示：你刚才只说了要做事但没有调用工具。请直接调用工具函数，不要只说'让我查询'。]",
                    "[系统警告：你连续两次只说不做！必须立即调用工具，否则无法完成任务。直接输出tool_call。]",
                    "[系统强制：这是最后一次机会。立刻调用terminal_execute或其他工具，不要再输出任何文字说明。]",
                ]
                for _ri, _prompt in enumerate(_retry_prompts):
                    retry_messages = full_messages + [response, HumanMessage(content=_prompt)]
                    _retry_result = [None]
                    _retry_error = [None]
                    def _do_retry(_msgs=retry_messages):
                        try:
                            _retry_result[0] = llm.invoke(_msgs)
                        except Exception as e:
                            _retry_error[0] = e
                    _retry_thread = threading.Thread(target=_do_retry, daemon=True)
                    _retry_thread.start()
                    while _retry_thread.is_alive():
                        if cancel_event and cancel_event.is_set():
                            return {"messages": [AIMessage(content="(请求已取消)")]}
                        _retry_thread.join(timeout=2)
                    if _retry_error[0]:
                        print(f"[{_ts}] [EMPTY-INTENT] 重试{_ri+1}失败: {str(_retry_error[0])[:200]}", flush=True)
                        break
                    response = _retry_result[0]
                    _resp_chars = len(str(response.content)) if response.content else 0
                    _resp_tc = ''
                    if hasattr(response, 'tool_calls') and response.tool_calls:
                        _resp_tc = f' tool_calls=[{", ".join(tc["name"] for tc in response.tool_calls)}]'
                        print(f"[{_ts}] [EMPTY-INTENT] 重试{_ri+1}成功: {_resp_chars} chars{_resp_tc}", flush=True)
                        break  # 成功生成tool_calls，退出重试
                    # 检查重试结果是否有实质内容
                    _retry_content = response.content if isinstance(response.content, str) else str(response.content or "")
                    _retry_has_substance = any(kw in _retry_content for kw in [
                        "：\n", ":\n", "```", "1.", "2.", "G:\\", "C:\\",
                        "详细如下", "列出如下", "找到以下", "结果如下",
                    ]) or len(_retry_content) >= 150
                    if _retry_has_substance:
                        print(f"[{_ts}] [EMPTY-INTENT] 重试{_ri+1}给出实质内容: {_resp_chars} chars", flush=True)
                        break
                    print(f"[{_ts}] [EMPTY-INTENT] 重试{_ri+1}仍无tool_call: '{_retry_content[:60]}'", flush=True)
                    # 继续下一次重试
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
    SAME_TOOL_LIMIT = 8       # 同工具调 8 次以上才考虑（给足探索空间）
    SAME_TOOL_HARD_LIMIT = 15 # 硬上限，无论如何超过就杀

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
                # 在进步 → 不杀，但在 LLM 上下文中注入提示让它收敛
                print(f"[{_ts}] [LOOP-DETECT] 同工具 {tool_name} 调用 {count} 次但在进步，注入收敛提示", flush=True)
                # 给最后一条 AI 消息追加提示，引导 LLM 总结现有结果
                if hasattr(last_message, "tool_calls") and last_message.tool_calls:
                    # 在 tool_call 的消息里塞一段 content 提示
                    _hint = (f"\n[系统警告：{tool_name} 已调用 {count} 次。"
                             f"请基于已有结果总结回答用户，不要再调用 {tool_name}。"
                             f"如果信息不足，直接告诉用户你找到了什么。]")
                    if last_message.content:
                        last_message.content += _hint
                    else:
                        last_message.content = _hint
                return "tools"  # 放行这次，让 LLM 看到提示后自己收敛
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
