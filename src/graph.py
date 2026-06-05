"""Agent 核心：LangGraph State + Graph 定义

架构参考 Hermes Agent，但用 LangGraph 图结构实现：

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

from typing import Annotated, Literal
from typing_extensions import TypedDict
from langgraph.graph import StateGraph, END
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage

from tools import ALL_TOOLS
from memory import Memory


# ── State 定义 ──────────────────────────────────────────────

class AgentState(TypedDict):
    """Agent 状态，贯穿整个图执行"""
    messages: Annotated[list, add_messages]  # 对话历史，自动合并


# ── LLM 初始化 ─────────────────────────────────────────────

def create_llm(model: str = None, base_url: str = None, api_key: str = None):
    """创建绑定工具的 LLM 实例"""
    import os
    from dotenv import load_dotenv
    load_dotenv()

    llm = ChatOpenAI(
        model=model or os.getenv("MODEL_NAME", "gpt-4o-mini"),
        base_url=base_url or os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        api_key=api_key or os.getenv("OPENAI_API_KEY"),
        temperature=0.3,
    )
    return llm.bind_tools(ALL_TOOLS)


# ── 图节点 ──────────────────────────────────────────────────

SYSTEM_PROMPT = """你是一个本地 AI Agent，可以操控用户的电脑来完成任务。

你的核心能力：
1. 执行终端命令（terminal_execute）
2. 读写文件（read_file, write_file）
3. 搜索文件（search_files）

工作原则：
- 直接执行，不要反复确认
- 遇到错误时自动修复重试
- 用中文回复用户
- 操作前简单说明你要做什么，然后执行
"""

# 记忆实例（模块级别）
_memory = Memory()


def agent_node(state: AgentState, llm) -> dict:
    """LLM 思考节点：决定下一步是调用工具还是回复用户"""
    messages = state["messages"]

    # 构建完整消息列表（system prompt + 记忆 + 对话历史）
    memory_context = _memory.get_context()
    system_content = SYSTEM_PROMPT
    if memory_context:
        system_content += f"\n\n{memory_context}"

    full_messages = [SystemMessage(content=system_content)] + messages
    response = llm.invoke(full_messages)
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

    # 工具执行节点
    tool_node = ToolNode(ALL_TOOLS)

    # 创建图
    graph = StateGraph(AgentState)

    # 添加节点
    graph.add_node("agent", lambda state: agent_node(state, llm))
    graph.add_node("tools", tool_node)

    # 设置入口
    graph.set_entry_point("agent")

    # 添加边
    graph.add_conditional_edges("agent", should_continue, {"tools": "tools", "end": END})
    graph.add_edge("tools", "agent")  # 工具执行后回到 agent 思考

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

    # 返回最后一条 AI 消息
    last_ai = None
    for msg in result["messages"]:
        if hasattr(msg, "content") and msg.type == "ai" and msg.content:
            last_ai = msg

    return last_ai.content if last_ai else "(无回复)"
