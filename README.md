# AI Agent — 基于 LangGraph 的本地 Agent

本地运行的 AI Agent，可以操控你的电脑执行命令、读写文件、搜索代码。

参考了 Hermes Agent 的核心架构（LLM循环 + 工具调用 + 持久记忆），用 LangGraph 图结构实现。

## 快速开始

```bash
# 1. 配置 API（编辑 .env 文件）
# 支持任何 OpenAI 协议兼容的 LLM 服务

# 2. 安装依赖
pip install -r requirements.txt

# 3. 运行
python src/main.py
```

## 交互命令

| 命令 | 说明 |
|------|------|
| `/quit` | 退出 |
| `/reset` | 重置对话 |
| `/memory` | 查看记忆 |
| `/help` | 显示帮助 |

## 架构

```
用户输入 → [Agent节点: LLM思考] → 有tool_call? → [ToolNode: 执行] → 回到Agent
                                    ↓ 无tool_call
                                  返回用户
```

### 核心组件

- **graph.py** — LangGraph StateGraph 定义（核心循环）
- **tools/** — 工具实现（终端执行、文件读写、搜索）
- **memory/** — JSON 持久记忆存储
- **main.py** — 交互式 CLI 入口

## 扩展方向

- [ ] Web 搜索工具
- [ ] 浏览器操控工具
- [ ] 技能系统（类似 Hermes Skills）
- [ ] 多 Agent 路由
- [ ] 微信/Telegram 接入（Gateway）
- [ ] 用本地 Qwen2.5-7B（你微调后的模型）替换 LLM