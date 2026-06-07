"""Agent 核心：LangGraph State + Graph 定义

架构参考 Hermes Agent，用 LangGraph 图结构实现：

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

SYSTEM_PROMPT = """你是一个本地 AI Agent，可以操控用户的电脑来完成任务。

你的核心能力：
1. 执行终端命令（terminal_execute）
2. 读写文件（read_file, write_file）
3. 搜索文件（search_files）
4. 网络搜索（web_search）
5. 持久记忆（memory_save, memory_search, memory_forget）— 你可以主动保存需要跨会话保留的信息

工作原则：
- 直接执行，不要反复确认
- 遇到错误时自动修复重试
- 用中文回复用户
- 回复简洁，不要啰嗦
- 当你发现值得记住的信息时（用户偏好、环境配置、经验教训），主动用 memory_save 保存
- 当遇到似曾相识的问题时，用 memory_search 查看是否有相关记忆
- **优先用工具验证事实**：当用户对某个结果提出质疑或追问时，主动执行相关命令去验证/深挖，不要只回复文字猜测
- **避免不必要的工具调用**：如果用户只是闲聊或问常识性问题，直接回答即可，不需要调用工具
"""

# 模块级单例
_memory = Memory()
_skill_manager = SkillManager(os.path.join(os.path.dirname(__file__), "..", "data", "skills"))


def agent_node(state: AgentState, llm) -> dict:
    """LLM 思考节点：决定下一步是调用工具还是回复用户"""
    messages = state["messages"]

    # 1. 上下文压缩：对话太长时自动摘要旧消息
    messages = compress_messages(messages, llm=None)  # 不用 LLM 摘要，用简单截断（省 token）

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
    try:
        response = llm.invoke(full_messages)
    except Exception as e:
        # LLM 调用失败（如 401 认证错误），直接返回错误消息，不再循环重试
        error_msg = str(e)
        if "401" in error_msg or "Authentication" in error_msg:
            return {"messages": [AIMessage(content="抱歉，AI 服务暂时认证失败，请稍后重试。")]}
        if "429" in error_msg or "rate" in error_msg.lower():
            return {"messages": [AIMessage(content="抱歉，AI 服务请求过于频繁，请稍后重试。")]}
        return {"messages": [AIMessage(content=f"抱歉，AI 服务调用出错，请稍后重试。错误：{error_msg[:100]}")]}
    return {"messages": [response]}


def should_continue(state: AgentState) -> Literal["tools", "end"]:
    """条件边：判断 LLM 是否要调用工具"""
    last_message = state["messages"][-1]
    if hasattr(last_message, "tool_calls") and last_message.tool_calls:
        return "tools"
    return "end"


# ── 构建图 ──────────────────────────────────────────────────

def build_graph(llm=None):
    """构建 LangGraph Agent 图"""
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
