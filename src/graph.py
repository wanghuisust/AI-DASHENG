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
import time
import concurrent.futures
from typing import Annotated, Literal
from typing_extensions import TypedDict
from langgraph.graph import StateGraph, END
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage, ToolMessage

from tools import ALL_TOOLS
from memory import Memory
from skills import SkillManager
from context_compress import compress_messages
from constants import estimate_tokens, get_max_context_tokens, trim_messages_to_tokens

# ── State 定义 ──────────────────────────────────────────────

class AgentState(TypedDict):
    """Agent 状态，贯穿整个图执行"""
    messages: Annotated[list, add_messages]
    session_start_index: int  # 本次请求开始时 messages 的长度（历史消息不算本次工具调用次数）


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

### 8. 工具超时/报错时自动换方式（关键！严格！）
- 当工具返回"[工具超时]"时，说明该操作耗时过长，**必须**换完全不同的方式，不要再用同工具重试
- 当工具返回"[工具执行失败]"时，说明该工具调用方式有问题，**必须**换完全不同的方式
- 当工具结果附带"[工具循环警告]"时，系统检测到你可能在重复失败的调用，请立即更换策略
- 当工具结果附带"[工具循环硬性中断]"时，系统已强制阻止你的调用，你必须换完全不同的工具或方法
- **绝对禁止**的行为：
  - ❌ 用同一个工具+类似参数重试（例如 terminal_execute 超时后又用 terminal_execute 换个命令）
  - ❌ 看到"超时"后只换参数不换工具（工具本身可能就不适合这个任务）
- **必须做**的行为：
  - ✅ terminal_execute 超时 → 直接放弃命令执行，改用 search_files/read_file 等本地工具
  - ✅ web_search 报错 → 用不同的搜索词或直接告知用户
  - ✅ 任何工具连续失败 → 直接回复用户当前情况和建议，不要继续尝试

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


# ── 工具调用守卫（Tool Call Guardrail）────────────────────────────────
# 参考 Hermes Agent 的 ToolCallGuardrailController，实现三层循环检测：
#   1. 精确重复（exact_failure）：同工具+同参数反复失败
#   2. 同类重复（same_tool_failure）：同工具名反复失败
#   3. 无进展（idempotent_no_progress）：幂等工具重复返回相同结果
# 核心理念：不硬性中断，而是将 guidance 附加到 tool result 中让 LLM 自己收敛；
# 只有 LLM 持续忽略提示时，才提升为 block/halt 强制中断。

import hashlib as _hashlib
import json as _json_mod

# 幂等工具（只读，不改变状态）——重复调用无进展是问题信号
_IDEMPOTENT_TOOLS = frozenset({
    "read_file", "search_files", "web_search", "web_extract",
    "session_search", "browser_snapshot", "browser_console",
    "browser_get_images", "list_directory",
})

# 变更工具（写入/执行）——重复调用可能是合理探索
_MUTATING_TOOLS = frozenset({
    "terminal_execute", "write_file", "create_file",
    "browser_click", "browser_type", "browser_press", "browser_navigate",
})


class _GuardrailDecision:
    """guardrail 决策：allow / warn / block / halt"""
    __slots__ = ("action", "code", "message", "tool_name", "count")

    def __init__(self, action="allow", code="allow", message="", tool_name="", count=0):
        self.action = action   # allow | warn | block | halt
        self.code = code       # 机器可读的决策代码
        self.message = message # 人可读的 guidance
        self.tool_name = tool_name
        self.count = count

    @property
    def allows_execution(self):
        return self.action in {"allow", "warn"}

    @property
    def should_halt(self):
        return self.action in {"block", "halt"}


class ToolCallGuardrail:
    """每次 agent turn 的工具调用守卫控制器

    参考 Hermes ToolCallGuardrailController：
    - before_call：检查是否应阻止本次调用（精确重复超过硬上限）
    - after_call：记录结果，分类失败/成功，决定 warn/halt
    - 合成结果：被阻止的调用返回合成错误，不让工具真正执行
    - guidance 附加：警告信息追加到真实 tool result 后，引导 LLM 换方式
    """

    # ── 阈值 ──
    EXACT_FAILURE_WARN_AFTER = 2     # 同参数失败2次 → 警告
    EXACT_FAILURE_BLOCK_AFTER = 4    # 同参数失败4次 → 阻止（原5→4，更快介入）
    SAME_TOOL_FAILURE_WARN_AFTER = 3 # 同工具名失败3次 → 警告
    SAME_TOOL_FAILURE_HALT_AFTER = 6 # 同工具名失败6次 → 强制中断（原8→6，防止超时-成功交替永远不到阈值）
    SAME_TOOL_CALL_HALT_AFTER = 20  # 同工具名调用20次 → 强制中断（不管成败，防止LLM反复用同一工具探索）
    NO_PROGRESS_WARN_AFTER = 2      # 幂等工具同结果2次 → 警告
    NO_PROGRESS_BLOCK_AFTER = 4     # 幂等工具同结果4次 → 阻止（原5→4）

    def __init__(self):
        self.reset()

    def reset(self):
        """新 turn 开始时重置"""
        self._exact_failure_counts = {}   # (tool_name, args_hash) → count
        self._same_tool_failure_counts = {} # tool_name → count
        self._tool_call_counts = {}       # tool_name → 总调用次数（不看成败）
        self._no_progress = {}             # (tool_name, args_hash) → (result_hash, repeat_count)
        self._halt_decision = None         # 第一次触发 halt 的决策

    @property
    def halt_decision(self):
        return self._halt_decision

    def before_call(self, tool_name: str, tool_args: dict) -> _GuardrailDecision:
        """工具调用前检查——如果精确重复失败超过硬上限，返回 block 决策"""
        args_hash = self._args_hash(tool_args)
        sig = (tool_name, args_hash)

        exact_count = self._exact_failure_counts.get(sig, 0)
        if exact_count >= self.EXACT_FAILURE_BLOCK_AFTER:
            decision = _GuardrailDecision(
                action="block",
                code="repeated_exact_failure_block",
                message=(
                    f"阻止 {tool_name}：同参数调用已失败 {exact_count} 次。"
                    "请更换策略或换用其他工具，不要用相同参数重试。"
                ),
                tool_name=tool_name,
                count=exact_count,
            )
            self._set_halt(decision)
            return decision

        # 幂等工具：同参数无进展超过阈值
        if tool_name in _IDEMPOTENT_TOOLS:
            record = self._no_progress.get(sig)
            if record is not None:
                _result_hash, repeat_count = record
                if repeat_count >= self.NO_PROGRESS_BLOCK_AFTER:
                    decision = _GuardrailDecision(
                        action="block",
                        code="idempotent_no_progress_block",
                        message=(
                            f"阻止 {tool_name}：该只读调用已返回相同结果 {repeat_count} 次。"
                            "请使用已有的结果，或换一个不同的查询方式。"
                        ),
                        tool_name=tool_name,
                        count=repeat_count,
                    )
                    self._set_halt(decision)
                    return decision

        return _GuardrailDecision(tool_name=tool_name)

    def after_call(self, tool_name: str, tool_args: dict, result: str, *, failed: bool) -> _GuardrailDecision:
        """工具调用后记录——分类失败/成功，决定 warn/halt，返回决策+guidance"""
        args_hash = self._args_hash(tool_args)
        sig = (tool_name, args_hash)

        # ── 追踪总调用次数（不看成败）──
        call_count = self._tool_call_counts.get(tool_name, 0) + 1
        self._tool_call_counts[tool_name] = call_count

        # 同工具名总调用超过硬上限 → halt（不管成败，防止LLM反复用同一工具探索）
        if call_count >= self.SAME_TOOL_CALL_HALT_AFTER:
            decision = _GuardrailDecision(
                action="halt",
                code="same_tool_call_overuse_halt",
                message=(
                    f"停止 {tool_name}：该工具已调用 {call_count} 次（不管成败），"
                    "远超正常范围。请彻底换一种不同的工具或方法，不要再调用此工具。"
                ),
                tool_name=tool_name,
                count=call_count,
            )
            self._set_halt(decision)
            print(f"[GUARDRAIL] HALT: {decision.code} tool={tool_name} call_count={call_count}", flush=True)
            return decision

        if failed:
            # ── 失败路径 ──
            exact_count = self._exact_failure_counts.get(sig, 0) + 1
            self._exact_failure_counts[sig] = exact_count
            self._no_progress.pop(sig, None)

            same_count = self._same_tool_failure_counts.get(tool_name, 0) + 1
            self._same_tool_failure_counts[tool_name] = same_count

            # 同工具名失败超过硬上限 → halt
            if same_count >= self.SAME_TOOL_FAILURE_HALT_AFTER:
                decision = _GuardrailDecision(
                    action="halt",
                    code="same_tool_failure_halt",
                    message=(
                        f"停止 {tool_name}：该工具已失败 {same_count} 次。"
                        "请彻底更换工具或方法，不要再沿此路径重试。"
                    ),
                    tool_name=tool_name,
                    count=same_count,
                )
                self._set_halt(decision)
                print(f"[GUARDRAIL] HALT: {decision.code} tool={tool_name} fail_count={same_count}", flush=True)
                return decision

            # 精确重复失败 → 警告
            if exact_count >= self.EXACT_FAILURE_WARN_AFTER:
                print(f"[GUARDRAIL] WARN: repeated_exact_failure tool={tool_name} exact_count={exact_count}", flush=True)
                return _GuardrailDecision(
                    action="warn",
                    code="repeated_exact_failure_warning",
                    message=(
                        f"[工具循环警告] {tool_name} 已用相同参数失败 {exact_count} 次。"
                        "请检查错误信息，更换参数或换用其他工具，不要重复相同调用。"
                    ),
                    tool_name=tool_name,
                    count=exact_count,
                )

            # 同工具名失败 → 警告
            if same_count >= self.SAME_TOOL_FAILURE_WARN_AFTER:
                hint = self._failure_hint(tool_name, same_count)
                print(f"[GUARDRAIL] WARN: same_tool_failure tool={tool_name} fail_count={same_count}", flush=True)
                return _GuardrailDecision(
                    action="warn",
                    code="same_tool_failure_warning",
                    message=hint,
                    tool_name=tool_name,
                    count=same_count,
                )

            return _GuardrailDecision(tool_name=tool_name, count=exact_count)

        # ── 成功路径 ──
        # 清零精确重复计数（同参数成功说明问题已解决）
        self._exact_failure_counts.pop(sig, None)

        # 注意：不清零 _same_tool_failure_counts！
        # 原因：一个工具可能"超时→成功→超时→成功"交替，如果成功就清零同工具失败计数，
        # 那失败计数永远到不了 halt 阈值，guardrail 形同虚设。
        # 同工具名失败计数只在整个 graph turn 结束时（reset）清零。

        # 变更工具：检查输出是否含去重标记（说明结果跟之前完全一样）
        if tool_name in _MUTATING_TOOLS:
            self._no_progress.pop(sig, None)
            # 变更工具输出含去重标记 → 视为无进展
            if "(同第" in (result or "") and "条消息" in (result or ""):
                dedup_count = self._same_tool_failure_counts.get(tool_name + "__dedup", 0) + 1
                self._same_tool_failure_counts[tool_name + "__dedup"] = dedup_count
                if dedup_count >= 3:
                    print(f"[GUARDRAIL] WARN: mutating_tool_dedup tool={tool_name} dedup_count={dedup_count}", flush=True)
                    return _GuardrailDecision(
                        action="warn",
                        code="mutating_tool_no_progress_warning",
                        message=(
                            f"[工具循环警告] {tool_name} 已连续 {dedup_count} 次返回重复输出。"
                            "说明你的命令没有产生新结果，请换完全不同的方式。"
                        ),
                        tool_name=tool_name,
                        count=dedup_count,
                    )
            return _GuardrailDecision(tool_name=tool_name)

        # 幂等工具：追踪结果是否重复
        if tool_name in _IDEMPOTENT_TOOLS:
            result_hash = self._result_hash(result)
            previous = self._no_progress.get(sig)
            repeat_count = 1
            if previous is not None and previous[0] == result_hash:
                repeat_count = previous[1] + 1
            self._no_progress[sig] = (result_hash, repeat_count)

            if repeat_count >= self.NO_PROGRESS_WARN_AFTER:
                return _GuardrailDecision(
                    action="warn",
                    code="idempotent_no_progress_warning",
                    message=(
                        f"[工具循环警告] {tool_name} 已返回相同结果 {repeat_count} 次。"
                        "请使用已有结果或换一个不同的查询，不要重复相同调用。"
                    ),
                    tool_name=tool_name,
                    count=repeat_count,
                )

        return _GuardrailDecision(tool_name=tool_name)

    def append_guidance(self, result: str, decision: _GuardrailDecision) -> str:
        """将 guardrail 的 guidance 附加到 tool result 中（Hermes 模式）"""
        if decision.action not in {"warn", "halt"} or not decision.message:
            return result
        label = "工具循环硬性中断" if decision.action == "halt" else "工具循环警告"
        return (result or "") + f"\n\n[{label}: {decision.code}; 次数={decision.count}; {decision.message}]"

    def synthetic_result(self, decision: _GuardrailDecision, tool_call_id: str) -> ToolMessage:
        """为被 block 的调用生成合成结果，替代真正的工具执行"""
        content = _json_mod.dumps({
            "error": decision.message,
            "guardrail": {
                "action": decision.action,
                "code": decision.code,
                "tool_name": decision.tool_name,
                "count": decision.count,
            }
        }, ensure_ascii=False)
        return ToolMessage(content=content, name=decision.tool_name, tool_call_id=tool_call_id)

    def _set_halt(self, decision: _GuardrailDecision):
        if decision.should_halt and self._halt_decision is None:
            self._halt_decision = decision

    @staticmethod
    def _args_hash(args: dict) -> str:
        canonical = _json_mod.dumps(args, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
        return _hashlib.sha256(canonical.encode()).hexdigest()[:16]

    @staticmethod
    def _result_hash(result: str) -> str:
        return _hashlib.sha256((result or "")[:2000].encode()).hexdigest()[:16]

    @staticmethod
    def _failure_hint(tool_name: str, count: int) -> str:
        base = (
            f"[工具循环警告] {tool_name} 已失败 {count} 次，疑似进入循环。"
            "不要放弃工具改用纯文本回复，而是先诊断错误再重试。"
        )
        if tool_name == "terminal_execute":
            return base + (
                "建议：先运行简单诊断命令（如 pwd && ls -la），再尝试绝对路径、"
                "更简单的命令、不同的工作目录，或换用 read_file/write_file。"
            )
        return base + (
            "建议：尝试不同的参数、更窄的查询范围、绝对路径，或换用能推进任务的其他工具。"
        )


# 模块级 guardrail 实例（每次 graph invoke 前由 agent_api.py 重置）
_tool_guardrail = ToolCallGuardrail()

def _reset_tool_guardrail():
    """重置 guardrail 状态（新 turn 开始时调用）"""
    _tool_guardrail.reset()

def _get_tool_guardrail() -> ToolCallGuardrail:
    return _tool_guardrail


# ── 工具超时配置 ──────────────────────────────────────────────
# 每个工具的执行超时（秒），有进展则每段继续等待，无进展则超时
TOOL_TIMEOUT_SECONDS = 20

# 工具名 → 超时覆盖（秒），None 表示使用默认 TOOL_TIMEOUT_SECONDS
# 对于已知耗时的工具（如 terminal_execute 自带 timeout 参数），给更长窗口
_TOOL_TIMEOUT_OVERRIDE = {
    "terminal_execute": 30,  # 终端命令自带 timeout 参数，但整体也要限制
}

# 工具执行最大等待次数（防止有进展但永远不结束的情况）
_MAX_TIMEOUT_ROUNDS = 10  # 最多等 10 轮 × 20s = 200s（对 terminal 是 10×30=300s）


def _execute_tool_with_timeout(tool_func, tool_name: str, tool_args: dict, tool_call_id: str, progress_callback=None) -> ToolMessage:
    """带 progress-aware 超时的工具执行包装器（参考 Hermes 改进版）

    核心逻辑（对应用户需求）：
    1. 工具调用报错 → 返回错误信息，提示 LLM 换用别的方式
    2. 等待工具返回时，每 TOOL_TIMEOUT_SECONDS 秒检查一次：
       - 如果有返回/进展 → 继续等待下一轮
       - 如果超时无进展 → 返回超时信息，提示 LLM 换工具
       - 如果报错 → 返回错误信息，提示换方式
    3. 防止永远不结束：最多 _MAX_TIMEOUT_ROUNDS 轮后强制超时

    与旧版区别：
    - 旧版是一次性 thread.join(timeout=20)，20s后无论是否有输出都判超时
    - 新版是分段等待，每段检查一次进展，有输出就继续等
    """
    timeout = _TOOL_TIMEOUT_OVERRIDE.get(tool_name, TOOL_TIMEOUT_SECONDS)
    import datetime as _dt
    _ts = _dt.datetime.now().strftime("%H:%M:%S")

    # 通知：工具开始执行
    if progress_callback:
        try:
            progress_callback("tool_started", {"tool": tool_name, "args": tool_args})
        except Exception:
            pass

    result_container = [None]   # [result_str]
    error_container = [None]    # [error_str]
    progress_container = [0]    # [已等待轮数]

    def _run_tool():
        try:
            result = tool_func.invoke(tool_args)
            result_container[0] = result
        except Exception as e:
            error_container[0] = str(e)

    t = threading.Thread(target=_run_tool, daemon=True)
    t.start()

    # ── 分段等待：每 timeout 秒检查一次进展 ──
    while True:
        t.join(timeout=timeout)

        if not t.is_alive():
            # 线程已结束
            break

        # 线程还活着 → 检查是否有部分结果
        progress_container[0] += 1
        round_num = progress_container[0]

        if result_container[0] is not None or error_container[0] is not None:
            # 有输出了但线程还活着（理论上不太可能，但防万一）
            break

        if round_num >= _MAX_TIMEOUT_ROUNDS:
            # 超过最大等待轮数，强制超时
            print(f"[{_ts}] [TOOL-TIMEOUT] {tool_name} 等待超过 {round_num}×{timeout}s，强制中断", flush=True)
            if progress_callback:
                try:
                    progress_callback("tool_timeout", {"tool": tool_name, "timeout": round_num * timeout})
                except Exception:
                    pass
            return ToolMessage(
                content=(
                    f"[工具超时] {tool_name} 执行超过 {round_num * timeout} 秒仍未返回。"
                    "该操作可能耗时过长，请换用其他方式完成（例如换一个工具、简化参数、分步执行）。"
                ),
                name=tool_name,
                tool_call_id=tool_call_id,
            )

        # 等待中但无进展 → 真正的超时（本次无进展）
        print(f"[{_ts}] [TOOL-TIMEOUT] {tool_name} 执行超过 {timeout}s 无进展（第{round_num}轮）", flush=True)
        if progress_callback:
            try:
                progress_callback("tool_timeout", {"tool": tool_name, "timeout": timeout})
            except Exception:
                pass
        return ToolMessage(
            content=(
                f"[工具超时] {tool_name} 执行超过 {timeout} 秒无进展。"
                "请换用其他方式完成此操作（例如换一个工具、换更简单的参数、或换一个不同的方法）。"
            ),
            name=tool_name,
            tool_call_id=tool_call_id,
            status="error",
        )

    # ── 线程已结束，处理结果 ──
    if error_container[0] is not None:
        # 工具报错：返回错误信息，提示换方式（使用头尾保留截断）
        from tools.tool_base import truncate_error
        err = truncate_error(error_container[0], head_size=2000, tail_size=2000)
        print(f"[{_ts}] [TOOL-ERROR] {tool_name} 执行报错: {error_container[0][:200]}", flush=True)
        if progress_callback:
            try:
                progress_callback("tool_error", {"tool": tool_name, "error": error_container[0][:200]})
            except Exception:
                pass
        return ToolMessage(
            content=(
                f"[工具执行失败] {tool_name} 报错: {err}\n\n"
                "请换用其他方式完成此操作（例如换一个工具、换更简单的参数、或换一个不同的方法）。"
            ),
            name=tool_name,
            tool_call_id=tool_call_id,
            status="error",
        )

    # 正常返回（包括验证错误等通过 _run() 返回的结果）
    result = result_container[0]
    if result is None:
        result = "(无输出)"
    _result_str = str(result)
    _result_preview = _result_str[:80].replace('\n', '\\n')
    _is_failed = _is_tool_failed(_result_str)
    print(f"[{_ts}] [TOOL-{'ERROR' if _is_failed else 'OK'}] {tool_name}: {_result_preview}", flush=True)
    if progress_callback:
        try:
            progress_callback("tool_done", {"tool": tool_name, "content": _result_str[:200]})
        except Exception:
            pass
    # status: 只接受 "error" 或不传（不能传 None）
    _msg_kwargs = dict(content=_result_str, name=tool_name, tool_call_id=tool_call_id)
    if _is_failed:
        _msg_kwargs["status"] = "error"
    return ToolMessage(**_msg_kwargs)

_tool_progress_callback = None  # 默认无回调（同步接口不设）

def _set_tool_progress_callback(callback):
    """设置工具执行进度回调（线程安全）"""
    global _tool_progress_callback
    _tool_progress_callback = callback

def _get_tool_progress_callback():
    """获取当前工具执行进度回调"""
    return _tool_progress_callback


def _is_tool_failed(content: str) -> bool:
    """判断工具结果是否属于失败（超时/报错/未找到/非零exit/编码乱码/Python异常）
    
    检测范围：
    1. 明确标记：[工具超时]、[工具执行失败]、[工具未找到]、[工具执行异常]
    2. 非零 exit code：[exit code: X] 且 X != 0
    3. stderr 含 error/Traceback/语法错误
    4. 编码乱码特征（GBK 解码错误产生的乱码如 椤圭洰、鏂囦欢）
    5. Python 异常输出（Traceback、SyntaxError、AttributeError 等）
    """
    if not content:
        return False
    # 1. 明确标记
    explicit_markers = ["[工具超时]", "[工具执行失败]", "[工具未找到]", "[工具执行异常]", "[验证错误]"]
    if any(marker in content for marker in explicit_markers):
        return True
    # 2. 非零 exit code
    import re as _re
    exit_match = _re.search(r'\[exit code:\s*(\d+)\]', content)
    if exit_match and int(exit_match.group(1)) != 0:
        return True
    # 3. stderr 含明显错误关键词
    if '[stderr]' in content:
        stderr_keywords = ['error', 'Error', 'ERROR', 'Traceback', 'fatal', 'FATAL',
                          'SyntaxError', 'AttributeError', 'TypeError', 'ValueError',
                          'ImportError', 'ModuleNotFoundError', 'KeyError', 'IndexError',
                          'FileNotFoundError', 'PermissionError', 'OSError']
        if any(kw in content for kw in stderr_keywords):
            return True
    # 4. 编码乱码特征（GBK→UTF-8 解码错误）
    garbled_markers = ['椤圭洰', '鏂囦欢', '鎻愪氦', '娴佺▼', '姝ラ', '缁撴灉']
    if any(g in content for g in garbled_markers):
        return True
    # 5. Python 异常输出（独立行开头的 Traceback）
    if 'Traceback (most recent call last)' in content:
        return True
    return False


def _execute_single_tool(tc: dict, tool_map: dict, guardrail: ToolCallGuardrail, progress_callback) -> ToolMessage:
    """执行单个工具调用，集成 guardrail 的 before_call / after_call

    参考 Hermes run_agent.py 的 _execute_tool_calls 逻辑：
    1. before_call → 如果 block，返回合成结果（不执行工具）
    2. 执行工具 → 拿到结果
    3. after_call → 分类失败/成功，附加 guidance
    """
    tool_name = tc["name"]
    tool_args = tc.get("args", {})
    tool_call_id = tc.get("id", "")

    # ── before_call 检查 ──
    pre_decision = guardrail.before_call(tool_name, tool_args)
    if not pre_decision.allows_execution:
        # 被 block → 返回合成结果，不让工具真正执行
        import datetime as _dt
        _ts = _dt.datetime.now().strftime("%H:%M:%S")
        print(f"[{_ts}] [GUARDRAIL-BLOCK] {tool_name}: {pre_decision.code} (count={pre_decision.count})", flush=True)
        result = guardrail.synthetic_result(pre_decision, tool_call_id)
        # guardrail block 也是错误
        if isinstance(result, ToolMessage) and result.status != "error":
            result = ToolMessage(
                content=result.content,
                name=result.name,
                tool_call_id=result.tool_call_id,
                status="error",
            )
        return result

    # ── 工具未找到 ──
    tool_func = tool_map.get(tool_name)
    if tool_func is None:
        result_msg = ToolMessage(
            content="[工具未找到] {tool_name} 不存在。请换用其他工具。",
            name=tool_name,
            tool_call_id=tool_call_id,
            status="error",
        )
        # 记录为失败
        guardrail.after_call(tool_name, tool_args, result_msg.content, failed=True)
        return result_msg

    # ── 执行工具 ──
    result_msg = _execute_tool_with_timeout(tool_func, tool_name, tool_args, tool_call_id, progress_callback=progress_callback)

    # ── after_call 分类 ──
    failed = _is_tool_failed(result_msg.content)
    decision = guardrail.after_call(tool_name, tool_args, result_msg.content, failed=failed)

    # 附加 guidance（warn/halt 时追加提示到结果）
    if decision.action in {"warn", "halt"} and decision.message:
        result_msg = ToolMessage(
            content=guardrail.append_guidance(result_msg.content, decision),
            name=result_msg.name,
            tool_call_id=result_msg.tool_call_id,
        )

    return result_msg


def tool_node_with_timeout(state: AgentState) -> dict:
    """自定义工具节点：替代 LangGraph 的 ToolNode，集成 guardrail + 超时保护

    参考 Hermes Agent 的工具执行架构：
    1. before_call：检查是否应阻止重复失败的调用
    2. 执行工具：带 progress-aware 超时
    3. after_call：分类失败/成功，附加 guidance 引导 LLM 换方式
    4. 并行执行多个工具调用（当 LLM 一次返回多个 tool_calls 时）
    """
    last_message = state["messages"][-1]
    if not (hasattr(last_message, "tool_calls") and last_message.tool_calls):
        return {"messages": []}

    # 获取当前进度回调和 guardrail
    _pcb = _get_tool_progress_callback()
    _gr = _get_tool_guardrail()

    # 构建工具名 → 工具函数 的映射
    tool_map = {}
    for t in ALL_TOOLS:
        tool_map[t.name] = t

    results = []

    # 如果只有一个工具调用，直接同步执行
    if len(last_message.tool_calls) == 1:
        tc = last_message.tool_calls[0]
        results.append(_execute_single_tool(tc, tool_map, _gr, _pcb))
    else:
        # 多个工具调用并行执行
        # 1. 先串行化 before_call（guardrail 非线程安全）
        blocked_results = {}   # tc_index -> ToolMessage
        allowed_tcs = []      # [(tc, original_index), ...]
        for i, tc in enumerate(last_message.tool_calls):
            pre_decision = _gr.before_call(tc["name"], tc.get("args", {}))
            if not pre_decision.allows_execution:
                import datetime as _dt
                _ts = _dt.datetime.now().strftime("%H:%M:%S")
                print(f"[{_ts}] [GUARDRAIL-BLOCK] {tc['name']}: {pre_decision.code}", flush=True)
                block_msg = _gr.synthetic_result(pre_decision, tc.get("id", ""))
                # 标记 status=error
                if isinstance(block_msg, ToolMessage) and block_msg.status != "error":
                    block_msg = ToolMessage(
                        content=block_msg.content,
                        name=block_msg.name,
                        tool_call_id=block_msg.tool_call_id,
                        status="error",
                    )
                blocked_results[i] = block_msg
            else:
                allowed_tcs.append((tc, i))

        # 2. 并行执行允许的调用（工具执行本身是线程安全的）
        parallel_results = {}  # original_index -> ToolMessage
        if allowed_tcs:
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(allowed_tcs), 4)) as executor:
                futures = {}
                for tc, orig_idx in allowed_tcs:
                    tool_func = tool_map.get(tc["name"])
                    if tool_func is None:
                        result_msg = ToolMessage(
                            content=f"[工具未找到] {tc['name']} 不存在。请换用其他工具。",
                            name=tc["name"],
                            tool_call_id=tc.get("id", ""),
                            status="error",
                        )
                        parallel_results[orig_idx] = result_msg
                    else:
                        future = executor.submit(
                            _execute_tool_with_timeout, tool_func, tc["name"],
                            tc.get("args", {}), tc.get("id", ""),
                            progress_callback=_pcb
                        )
                        futures[future] = (tc, orig_idx)

                # 收集并行结果
                for future in concurrent.futures.as_completed(futures, timeout=120):
                    tc, orig_idx = futures[future]
                    try:
                        result_msg = future.result(timeout=5)
                        parallel_results[orig_idx] = result_msg
                    except Exception as e:
                        _tcid = tc.get("id", "")
                        parallel_results[orig_idx] = ToolMessage(
                            content=f"[工具执行异常] {e}。请换用其他方式完成此操作。",
                            name="unknown",
                            tool_call_id=_tcid,
                            status="error",
                        )

        # 3. 串行化 after_call（guardrail 非线程安全）+ 按 tool_call 顺序排列
        for i, tc in enumerate(last_message.tool_calls):
            if i in blocked_results:
                results.append(blocked_results[i])
                _gr.after_call(tc["name"], tc.get("args", {}),
                               blocked_results[i].content, failed=True)
            elif i in parallel_results:
                result_msg = parallel_results[i]
                failed = _is_tool_failed(result_msg.content)
                decision = _gr.after_call(tc["name"], tc.get("args", {}),
                                          result_msg.content, failed=failed)
                if decision.action in {"warn", "halt"} and decision.message:
                    result_msg = ToolMessage(
                        content=_gr.append_guidance(result_msg.content, decision),
                        name=result_msg.name,
                        tool_call_id=result_msg.tool_call_id,
                        status=result_msg.status if hasattr(result_msg, 'status') else None,
                    )
                results.append(result_msg)

    # P0-1: 聚合工具结果预算（迁移自 Claude Code enforceToolResultBudget）
    from tools.tool_base import enforce_tool_result_budget
    results = enforce_tool_result_budget(results)

    return {"messages": results}


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


def _halt_summarize(messages: list, reason: str) -> str:
    """guardrail HALT 或绝对上限时，做一次无工具 LLM 总结。
    避免用户只看到中间轮的碎片文本（如"让我试试另一种方法"）。
    """
    try:
        llm = create_llm()
        # 去掉工具绑定，纯文本模式
        from langchain_openai import ChatOpenAI
        import os
        from dotenv import load_dotenv
        from pathlib import Path
        env_path = Path(__file__).resolve().parent.parent / ".env"
        load_dotenv(env_path, override=True)
        summary_llm = ChatOpenAI(
            model=os.getenv("MODEL_NAME", "gpt-4o-mini"),
            base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            api_key=os.getenv("OPENAI_API_KEY"),
            temperature=0.3,
            request_timeout=30,
            max_retries=2,
        )
        # 构造总结消息：system + 最近几轮对话
        summary_msgs = []
        # system prompt
        if messages and hasattr(messages[0], "type") and messages[0].type == "system":
            summary_msgs.append(messages[0])
        # 只取最近 10 条消息（避免 token 过多）
        recent = messages[-10:]
        summary_msgs.extend(recent)
        summary_msgs.append(HumanMessage(content=(
            f"⚠️ {reason}\n\n"
            "请根据以上对话历史，总结你目前完成了什么、还有什么没完成、"
            "以及用户下一步可以怎么做。用简洁的中文回复，不要使用任何工具。"
        )))
        result = summary_llm.invoke(summary_msgs)
        return result.content if hasattr(result, "content") else str(result)
    except Exception as e:
        print(f"[HALT-SUMMARIZE] 总结失败: {e}", flush=True)
        return ""


def should_continue(state: AgentState) -> Literal["tools", "end"]:
    """条件边：判断 LLM 是否要调用工具，结合 guardrail 检测死循环

    参考 Hermes Agent 架构简化：
    - 循环检测的精确/同类/无进展三层判断已移至 ToolCallGuardrail
    - guardrail 在工具执行前后（before_call/after_call）已做了 warn/block/halt
    - should_continue 只需：1) 检查 guardrail 是否已 halt；2) 兜底安全上限
    """
    last_message = state["messages"][-1]
    if not (hasattr(last_message, "tool_calls") and last_message.tool_calls):
        return "end"

    import datetime as _dt
    _ts = _dt.datetime.now().strftime("%H:%M:%S")

    # ── 优先检查：guardrail 是否已触发 halt ──
    _gr = _get_tool_guardrail()
    halt = _gr.halt_decision
    if halt is not None:
        print(f"[{_ts}] [GUARDRAIL-HALT] {halt.tool_name}: {halt.code} (count={halt.count})", flush=True)
        last_message.tool_calls = []
        # HALT 时做一次无工具总结，避免用户只看到中间轮碎片文本
        halt_reason = f"{halt.tool_name} 连续失败 {halt.count} 次，已停止尝试该工具"
        summary = _halt_summarize(state["messages"], halt_reason)
        halt_hint = f"\n\n⚠️ 操作受限：{halt_reason}。"
        if summary:
            last_message.content = summary + halt_hint
            print(f"[{_ts}] [GUARDRAIL-HALT] 总结完成: {last_message.content[:80]}...", flush=True)
        else:
            last_message.content = (
                f"{halt.tool_name} 连续失败 {halt.count} 次，"
                "请换一种完全不同的方式（例如换一个完全不同的工具、换一个完全不同的思路），"
                "或者告诉我具体要做什么，我来想办法。"
            )
        return "end"

    # ── 兜底安全上限：只计数本次请求新增的工具调用 ──
    # 防止 guardrail 阈值宽松时 LLM 一直在"进步"但永远不会停
    # 注意：恢复的历史消息中的工具调用不算本次，避免旧历史吃掉额度
    start_idx = state.get("session_start_index", 0)
    session_tool_results = sum(
        1 for msg in state["messages"][start_idx:]
        if hasattr(msg, "type") and msg.type == "tool"
    )
    ABSOLUTE_TOOL_LIMIT = 100
    if session_tool_results >= ABSOLUTE_TOOL_LIMIT:
        print(f"[{_ts}] [ABSOLUTE-LIMIT] 本次工具调用次数 {session_tool_results} 达到上限 {ABSOLUTE_TOOL_LIMIT}", flush=True)
        last_message.tool_calls = []
        limit_reason = "本次对话的工具调用次数已达上限，已自动停止"
        summary = _halt_summarize(state["messages"], limit_reason)
        limit_hint = f"\n\n⚠️ {limit_reason}。"
        if summary:
            last_message.content = summary + limit_hint
        else:
            last_message.content = "抱歉，本次对话的工具调用次数已达上限，已自动停止。请总结当前进展后重新开始。"
        return "end"

    return "tools"


# ── 构建图 ──────────────────────────────────────────────────

def build_graph(llm=None, cancel_event: threading.Event = None):
    """构建 Agent 图
    cancel_event: 传入取消信号，LLM invoke 等待期间可被中断
    """
    # 新 session → 重置 FileStateCache（Read-Before-Edit 状态不应跨 session）
    from tools import reset_file_state_cache
    reset_file_state_cache()

    if llm is None:
        llm = create_llm()

    # 使用带超时+重试的自定义工具节点，替代原始 ToolNode
    # 原始 ToolNode 无超时机制，工具卡住会阻塞整个图执行
    graph = StateGraph(AgentState)
    graph.add_node("agent", lambda state: agent_node(state, llm, cancel_event))
    graph.add_node("tools", tool_node_with_timeout)
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
