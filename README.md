# AI-DASHENG — 本地 AI Agent

本地运行的 AI Agent，可以操控你的电脑执行命令、读写文件、搜索文件、网络搜索。支持 QQ Bot 消息接入。

## 架构

```
                    ┌──────────────────────────────────────────┐
                    │           DASHENG 系统架构               │
                    └──────────────────────────────────────────┘

  QQ / 微信 ───▶ Gateway (:9090) ───▶ Agent API (:8900)
                    │                      │
                    │  消息路由 + 进度推送      │  Agent 推理循环
                    │                      │
                    │                      ├─▶ LLM 思考 (OpenAI 协议)
                    │                      │      │
                    │                      │      ├─ tool_calls → 工具执行
                    │                      │      │       │
                    │                      │       │  ├─ terminal_execute (终端命令)
                    │                      │       │  ├─ read_file / write_file (文件读写)
                    │                      │       │  ├─ search_files (文件搜索)
                    │                      │       │  ├─ web_search (网络搜索)
                    │                      │       │  └─ memory_save/search/forget (持久记忆)
                    │                      │       │
                    │                      │      └─ 纯文本 → 返回用户
                    │                      │
                    └─── WebServer (:7860) ──▶ Dashboard UI + Chat
```

**三个服务：**
| 服务 | 端口 | 说明 |
|------|------|------|
| Agent API | 8900 | 核心 Agent 推理 |
| WebServer | 7860 | Web Dashboard + Chat UI |
| Gateway | 9090 | QQ/微信消息路由 + 进度推送 |

## 一键安装（Windows）

### 前置条件

- **Python 3.10+**（已安装并可用 `python` 命令）
- **Git**（用于克隆仓库）
- 一个 **OpenAI 协议兼容的 LLM API Key**（OpenAI / DeepSeek / 通义千问 / 本地模型均可）

### 步骤 1：克隆仓库

```bash
# 直连
git clone https://github.com/wanghuisust/AI-DASHENG.git
cd AI-DASHENG

# 如果无法访问 GitHub，使用镜像
git clone https://gh-proxy.com/https://github.com/wanghuisust/AI-DASHENG.git
cd AI-DASHENG
```

### 步骤 2：一键安装依赖

```bash
python dasheng.py install
```

这一步会自动完成：
- ✅ 创建 `.venv` 虚拟环境
- ✅ 安装所有 Python 依赖
- ✅ 创建数据目录
- ✅ 注册 `dasheng` 全局命令
- ✅ 设置 `DASHENG_HOME` 环境变量

### 步骤 3：配置 API

```bash
python dasheng.py setup
```

交互式配置向导，依次填入：
1. **LLM API** — 选择提供商 → 输入 API Key → 确认模型名称
2. **QQ Bot**（可选）— 输入 App ID 和 App Secret，或按 Enter 跳过
3. **微信**（可选）— 按 Enter 跳过

也可以手动配置：

```bash
# 复制模板
cp .env.example .env

# 编辑 .env，填入你的 API Key
# OPENAI_API_KEY=sk-xxx
# OPENAI_BASE_URL=https://api.openai.com/v1
# MODEL_NAME=gpt-4o-mini
```

### 步骤 4：启动服务

```bash
# 启动所有服务（Agent API + WebServer + Gateway）
python dasheng.py start

# 或仅启动 Agent API（开发调试用）
python dasheng.py start --agent-only
```

### 步骤 5：验证

```bash
# 查看服务状态
python dasheng.py status

# 在 Web UI 对话测试
# 打开 http://127.0.0.1:7860

# 或命令行直接对话
python dasheng.py chat
```

## 常用命令

```bash
python dasheng.py install   # 安装依赖
python dasheng.py setup     # 配置向导
python dasheng.py start     # 启动服务
python dasheng.py stop      # 停止服务
python dasheng.py restart   # 重启服务
python dasheng.py status    # 查看状态
python dasheng.py chat      # 命令行对话
```

## 工具列表

| 工具 | 功能 | 实现 |
|------|------|------|
| `terminal_execute` | 执行终端命令 | `subprocess.run()` |
| `read_file` | 读取文件内容 | Python 内置 `open()` |
| `write_file` | 写入文件 | Python 内置 `open()` |
| `search_files` | 搜索文件 | `glob.glob()` |
| `web_search` | 网络搜索 | DuckDuckGo |
| `memory_save` | 保存持久记忆 | 本地 JSON |
| `memory_search` | 搜索记忆 | 本地 JSON |
| `memory_forget` | 删除记忆 | 本地 JSON |

## QQ Bot 接入

1. 前往 [q.qq.com](https://q.qq.com) 创建 QQ 机器人
2. 获取 **App ID** 和 **App Secret**
3. 运行 `python dasheng.py setup` 或在 `.env` 中填入：
   ```
   QQ_APP_ID=你的AppID
   QQ_APP_SECRET=你的AppSecret
   ```
4. 启动服务后，Gateway 会自动连接 QQ WebSocket 网关
5. 在 QQ 上私聊机器人即可对话

**进度推送：** 当 Agent 处理时间较长时，会每 60 秒推送当前步骤状态：
- 🤔 正在思考...
- 🔧 正在调用: terminal_execute
- ✅ terminal_execute 执行完成
- 💭 推理中: ...

## 支持的 LLM 服务

任何 **OpenAI 协议兼容** 的 LLM 均可使用：

| 提供商 | Base URL | 推荐模型 |
|--------|----------|----------|
| OpenAI | `https://api.openai.com/v1` | gpt-4o-mini |
| 通义千问 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | qwen-plus |
| DeepSeek | `https://api.deepseek.com/v1` | deepseek-chat |
| 智谱 GLM | `https://open.bigmodel.cn/api/paas/v4` | glm-4-flash |
| Moonshot | `https://api.moonshot.cn/v1` | moonshot-v1-8k |
| SiliconFlow | `https://api.siliconflow.cn/v1` | Qwen/Qwen2.5-7B-Instruct |
| Agnes AI | `https://apihub.agnes-ai.com/v1` | agnes-2.0-flash |
| 本地模型 | `http://127.0.0.1:8080/v1` | — |

本地模型支持：llama.cpp、Ollama、vLLM 等，只需启动 OpenAI 兼容 API 即可。

## 配置说明

所有配置在 `.env` 文件中：

```env
# LLM 配置
OPENAI_API_KEY=sk-xxx           # API Key
OPENAI_BASE_URL=https://api.openai.com/v1  # API 地址
MODEL_NAME=gpt-4o-mini          # 模型名称
MODEL_CONTEXT_LENGTH=128000     # 上下文长度（token）

# QQ Bot 配置
QQ_APP_ID=                      # QQ 机器人 App ID
QQ_APP_SECRET=                  # QQ 机器人 App Secret

# 功能开关
ENABLE_TOOLS=true               # 是否启用工具调用
GATEWAY_PORT=9090               # Gateway 端口
```

## 项目结构

```
AI-DASHENG/
├── dasheng.py              # CLI 入口（install/setup/start/stop/status/chat）
├── .env.example            # 配置模板
├── requirements.txt        # Python 依赖
├── start_all_hidden.vbs    # Windows 后台启动脚本（自动生成）
├── src/
│   ├── agent_api.py        # Agent API 服务 (:8900)
│   ├── graph.py            # Agent 图定义
│   ├── web_server.py       # Web Dashboard (:7860)
│   ├── persistence.py      # 消息持久化（SQLite）
│   ├── context_compress.py # 上下文压缩
│   ├── constants.py        # 常量定义
│   ├── memory.py           # 持久记忆
│   ├── skills.py           # 技能管理
│   ├── tools/              # 工具函数
│   │   ├── terminal_tool.py
│   │   ├── file_tool.py
│   │   ├── search_tool.py
│   │   ├── web_search_tool.py
│   │   └─ memory_tool.py
│   └── gateway/            # 消息网关
│       ├── server.py       # Gateway 服务 (:9090)
│       └── qq_adapter.py   # QQ Bot 适配器
└── data/
    ├── conversations.db    # 对话历史（SQLite）
    ├── memory.json         # 持久记忆
    └── skills/             # 技能数据
```

## 开机自启（Windows）

```bash
# 启动后会自动生成 start_all_hidden.vbs
# 将其放入 Windows 启动目录即可：
# Win+R → shell:startup → 把 start_all_hidden.vbs 快捷方式放进去
```

## License

MIT
