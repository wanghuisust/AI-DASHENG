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
from constants import estimate_tokens, MAX_CONTEXT_TOKENS, trim_messages_to_tokens

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
    load_dotenv(env_path)

    llm = ChatOpenAI(
        model=model or os.getenv("MODEL_NAME", "gpt-4o-mini"),
        base_url=base_url or os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        api_key=api_key or os.getenv("OPENAI_API_KEY"),
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


def agent_node(state: AgentState, llm) -> dict:
    """LLM 思考节点：决定下一步是调用工具还是回复用户"""
    messages = state["messages"]

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
        response = llm.invoke(full_messages)
        _resp_chars = len(str(response.content)) if response.content else 0
        _resp_tc = ''
        if hasattr(response, 'tool_calls') and response.tool_calls:
            _resp_tc = f' tool_calls=[{", ".join(tc["name"] for tc in response.tool_calls)}]'
        print(f"[{_ts}] [LLM] invoke OK: {_resp_chars} chars{_resp_tc}", flush=True)
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
    """条件边：判断 LLM 是否要调用工具"""
    last_message = state["messages"][-1]
    if hasattr(last_message, "tool_calls") and last_message.tool_calls:
        return "tools"
    return "end"


# ── 构建图 ──────────────────────────────────────────────────

def build_graph(llm=None):
    """构建 Agent 图"""
    if llm is None:
        llm = create_llm()

    tool_node = ToolNode(ALL_TOOLS)

    graph = StateGraph(AgentState)
    graph.add_node("agent", lambda state: agent_node(state, llm))
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
