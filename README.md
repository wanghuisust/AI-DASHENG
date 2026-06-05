# AI-DASHENG — 基于 LangGraph 的本地 AI Agent

本地运行的 AI Agent，可以操控你的电脑执行命令、读写文件、搜索代码。

参考了 Hermes Agent 的核心架构（LLM循环 + 工具调用 + 持久记忆），用 LangGraph 图结构实现。

## 架构

```
用户输入 → LLM思考 → 判断是否需要工具
                ├─ 是 → 执行工具 → 结果回传LLM → 继续思考
                └─ 否 → 直接回复用户
```

## 工具

| 工具 | 功能 |
|------|------|
| `terminal_execute` | 执行终端命令 |
| `read_file` | 读取文件内容 |
| `write_file` | 写入文件 |
| `search_files` | 搜索文件 |

## 快速开始

```bash
# 1. 配置 API（编辑 .env 文件）
# 支持任何 OpenAI 协议兼容的 LLM 服务
cp .env.example .env

# 2. 安装依赖
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt

# 3. 启动 Web 界面
.venv\Scripts\python.exe src\web_server.py
# 打开 http://127.0.0.1:7860

# 或启动命令行交互
.venv\Scripts\python.exe src\main.py
```

## 配置说明

编辑 `.env`：

```env
OPENAI_API_KEY=your-key        # API Key
OPENAI_BASE_URL=http://127.0.0.1:8080/v1  # LLM 服务地址
MODEL_NAME=Qwen3.6             # 模型名称
```

支持本地模型（llama.cpp / vLLM / Ollama 等 OpenAI 兼容服务）和远程 API（OpenAI / DeepSeek 等）。
