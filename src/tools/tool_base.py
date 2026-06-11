"""工具基类 + 工厂 + 结果处理 — DASHENG Tool Protocol

迁移自 Claude Code 的 Tool Protocol + buildTool() + 结果持久化设计：
- DashengTool: 工具基类，定义 name/call/validate_input/is_read_only/max_result_size 等
- build_tool(): 工厂函数，安全默认值（fail-closed）
- process_tool_result(): 框架级结果大小处理（超限持久化+预览）

核心原则（来自 Claude Code）：
1. Fail-closed: 默认不可并发、默认写入需谨慎、默认需要验证
2. 防御纵深: Schema验证 → 业务验证(validate_input) → 工具执行 → 结果处理
3. 透明持久化: 工具不关心结果大小，框架自动处理
"""

import os
import json
import hashlib
from typing import Any, Callable, Optional
from pydantic import BaseModel, ValidationError

from langchain_core.tools import BaseTool
from langchain_core.messages import ToolMessage


# ── 结果大小常量（迁移自 Claude Code constants/toolLimits.ts）──

DEFAULT_MAX_RESULT_SIZE_CHARS = 50_000    # 单工具结果上限（字符）
MAX_PER_MESSAGE_CHARS = 200_000           # 单轮所有工具结果总计上限
PREVIEW_SIZE = 2_000                      # 持久化后的预览大小
TOOL_RESULTS_DIR = "tool-results"         # 持久化结果存放子目录


# ── 验证结果 ──

class ValidationResult:
    """validate_input 的返回值"""
    __slots__ = ("valid", "message")

    def __init__(self, valid: bool = True, message: str = ""):
        self.valid = valid
        self.message = message

    @staticmethod
    def ok():
        return ValidationResult(valid=True)

    @staticmethod
    def deny(message: str):
        return ValidationResult(valid=False, message=message)


# ── 结果持久化 ──

def _get_session_dir() -> str:
    """获取当前 session 的工具结果持久化目录"""
    from tools.tmp_manager import get_tmp_dir_path
    base = get_tmp_dir_path()
    results_dir = os.path.join(base, TOOL_RESULTS_DIR)
    os.makedirs(results_dir, exist_ok=True)
    return results_dir


def _persist_result(content: str, tool_use_id: str) -> str:
    """将大结果持久化到磁盘，返回预览消息"""
    session_dir = _get_session_dir()
    safe_id = hashlib.sha256(tool_use_id.encode()).hexdigest()[:16]
    filepath = os.path.join(session_dir, f"{safe_id}.txt")

    try:
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)
    except OSError as e:
        return content[:DEFAULT_MAX_RESULT_SIZE_CHARS] + \
            f"\n\n... (持久化失败: {e}，已截断至 {DEFAULT_MAX_RESULT_SIZE_CHARS} 字符)"

    size_kb = len(content) / 1024
    preview = content[:PREVIEW_SIZE]
    return (
        f"<persisted-output>\n"
        f"输出过大 ({size_kb:.1f} KB)，完整内容已保存到: {filepath}\n\n"
        f"预览（前 {PREVIEW_SIZE} 字符）:\n{preview}\n...\n"
        f"</persisted-output>"
    )


def process_tool_result(content: str, tool_name: str, tool_use_id: str,
                        max_result_size: int) -> str:
    """框架级结果大小处理（迁移自 Claude Code toolResultStorage.ts）

    工具 call() 返回的结果先经过此函数处理：
    - 大小 ≤ max_result_size → 直接返回
    - 大小 > max_result_size → 持久化到磁盘 + 返回预览
    - max_result_size=Infinity → 永不持久化（用于 read_file，防循环）
    """
    if not isinstance(content, str):
        content = str(content)

    if max_result_size == float("inf") or len(content) <= max_result_size:
        return content

    print(f"[TOOL-RESULT] {tool_name} 输出 {len(content)} 字符超过上限 "
          f"{max_result_size}，持久化到磁盘", flush=True)
    return _persist_result(content, tool_use_id)


# ── DashengTool 基类 ──

class DashengTool(BaseTool):
    """DASHENG 工具基类（迁移自 Claude Code Tool Protocol）

    关键字段：
    - name: 工具唯一名称
    - description: 工具描述（LLM 可见）
    - max_result_size: 结果大小上限，超限自动持久化（默认 50K）
    - is_read_only_flag: 是否只读工具（用于分类）
    - is_concurrency_safe_flag: 是否可并行执行

    生命周期：
    1. LLM 返回 tool_use → LangGraph 调用 _run() / _arun()
    2. _run() 内部：
       a. Pydantic Schema 自动验证（BaseTool 已内置）
       b. validate_input() 业务逻辑验证
       c. call() 执行核心逻辑
       d. process_tool_result() 结果大小处理
    """

    # ── Pydantic 字段（必须在类体声明）──
    max_result_size: int | float = DEFAULT_MAX_RESULT_SIZE_CHARS  # float("inf") = 永不持久化
    is_read_only_flag: bool = False
    is_concurrency_safe_flag: bool = False

    # ── 内部存储（非 Pydantic 字段，用 __dict__ 绕过验证）──
    # _func 和 _validate_fn 通过 build_tool 设置

    def validate_input(self, **kwargs) -> ValidationResult:
        """业务逻辑验证（Schema 之外的语义检查）

        子类可覆盖。默认通过。
        典型用途：
        - 文件操作：检查 Read-Before-Edit
        - 路径验证：检查设备文件、敏感路径
        - 参数语义：检查 old_string 是否存在
        """
        # 如果 build_tool 提供了验证函数，使用它
        validate_fn = self.__dict__.get('_validate_fn')
        if validate_fn is not None:
            return validate_fn(**kwargs)
        return ValidationResult.ok()

    def call(self, **kwargs) -> str:
        """工具核心逻辑

        如果 build_tool 提供了 func，使用它；否则子类必须覆盖。
        """
        func = self.__dict__.get('_func')
        if func is not None:
            return func(**kwargs)
        raise NotImplementedError("子类必须实现 call() 或通过 build_tool 提供 func")

    def _run(self, **kwargs) -> str:
        """LangGraph BaseTool 入口 — 编排完整生命周期"""

        # 1. 业务逻辑验证
        validation = self.validate_input(**kwargs)
        if not validation.valid:
            return f"[验证错误] {validation.message}"

        # 2. 执行核心逻辑
        result = self.call(**kwargs)

        # 3. 结果大小处理（框架级，工具不需要关心）
        args_hash = hashlib.sha256(
            json.dumps(kwargs, sort_keys=True, default=str, ensure_ascii=False).encode()
        ).hexdigest()[:16]
        tool_use_id = f"{self.name}_{args_hash}"

        return process_tool_result(result, self.name, tool_use_id, self.max_result_size)

    async def _arun(self, **kwargs) -> str:
        """异步入口 — 当前同步执行即可"""
        return self._run(**kwargs)


# ── build_tool 工厂函数 ──

def build_tool(
    name: str,
    description: str,
    func: Callable,
    args_schema: type[BaseModel] = None,
    max_result_size: int = DEFAULT_MAX_RESULT_SIZE_CHARS,
    is_read_only: bool = False,
    is_concurrency_safe: bool = False,
    validate_input: Callable = None,
) -> DashengTool:
    """工具工厂 — 从函数快速创建 DashengTool 实例

    设计原则（来自 Claude Code buildTool）：
    - 安全默认值：is_read_only=False, is_concurrency_safe=False
    - 最少参数：只需 name, description, func
    - 可选扩展：validate_input, args_schema, max_result_size

    Args:
        name: 工具名
        description: 工具描述（LLM 可见）
        func: 核心逻辑函数，接收关键字参数，返回 str
        args_schema: Pydantic BaseModel 类（用于参数验证 + schema 生成）
        max_result_size: 结果大小上限，默认 50K
        is_read_only: 是否只读工具
        is_concurrency_safe: 是否可并行执行
        validate_input: 业务验证函数，接收 **kwargs，返回 ValidationResult

    Returns:
        DashengTool 实例
    """
    # 构建 tool_schema（如果没有提供）
    if args_schema is None:
        from langchain_core.tools import tool as lc_tool
        temp_tool = lc_tool(func)
        if hasattr(temp_tool, 'args_schema') and temp_tool.args_schema:
            args_schema = temp_tool.args_schema
        if not name:
            name = temp_tool.name
        if not description:
            description = temp_tool.description or ""

    # 创建实例 — 所有 Pydantic 字段在构造函数中传入
    init_kwargs = dict(
        name=name,
        description=description,
        max_result_size=max_result_size,
        is_read_only_flag=is_read_only,
        is_concurrency_safe_flag=is_concurrency_safe,
    )
    if args_schema:
        init_kwargs["args_schema"] = args_schema

    instance = DashengTool(**init_kwargs)

    # 非Pydantic字段：存到 __dict__（绕过 Pydantic 验证）
    instance.__dict__['_func'] = func
    if validate_input:
        instance.__dict__['_validate_fn'] = validate_input

    return instance