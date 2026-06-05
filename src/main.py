"""AI Agent 主入口 - 交互式命令行界面"""

import sys
import os

# 确保 src 在搜索路径中
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv()

from rich.console import Console
from rich.panel import Panel
from rich.markdown import Markdown
from langchain_core.messages import HumanMessage, AIMessage

from graph import build_graph
from memory import Memory

console = Console()


def interactive_chat():
    """交互式聊天循环"""
    console.print(Panel(
        "[bold green]AI Agent[/] v0.1.0 — 基于 LangGraph 的本地 Agent\n"
        "输入消息开始对话，输入 /quit 退出，/reset 重置对话",
        title="🤖 AI Agent",
        border_style="green",
    ))

    # 初始化
    graph = build_graph()
    memory = Memory()
    messages = []

    while True:
        try:
            # 读取用户输入
            user_input = console.input("[bold cyan]你:[/] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]再见！[/]")
            break

        if not user_input:
            continue

        # 命令处理
        if user_input == "/quit":
            console.print("[dim]再见！[/]")
            break
        elif user_input == "/reset":
            messages = []
            console.print("[yellow]对话已重置[/]")
            continue
        elif user_input == "/memory":
            data = memory.list_all()
            console.print(Panel(str(data), title="🧠 记忆", border_style="blue"))
            continue
        elif user_input == "/help":
            console.print("""
[bold]命令:[/]
  /quit   — 退出
  /reset  — 重置对话
  /memory — 查看记忆
  /help   — 显示帮助
""")
            continue

        # 调用 Agent
        messages.append(HumanMessage(content=user_input))

        with console.status("[bold green]思考中...[/]", spinner="dots"):
            try:
                result = graph.invoke({"messages": messages})
                messages = result["messages"]

                # 找到最后一条 AI 文本回复
                last_ai = None
                for msg in messages:
                    if hasattr(msg, "type") and msg.type == "ai" and msg.content:
                        last_ai = msg

                if last_ai:
                    console.print()
                    console.print(Markdown(last_ai.content))
                    console.print()

            except Exception as e:
                console.print(f"[bold red]错误:[/] {e}")
                # 移除失败的用户消息，避免污染上下文
                messages.pop()


if __name__ == "__main__":
    interactive_chat()
