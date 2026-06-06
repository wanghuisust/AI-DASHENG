---
name: python-debug
description: Python 代码调试与错误修复
triggers: [python错误, debug, 报错, traceback, 调试python, 修复]
tools: [terminal_execute, read_file, write_file]
---

# Python 调试

1. **读取错误信息**：让用户提供 traceback 或从日志读取
2. **定位文件**：用 `read_file` 查看相关代码
3. **分析原因**：常见错误类型速查：
   - `ModuleNotFoundError` → `pip install xxx`
   - `FileNotFoundError` → 检查路径、编码问题（Windows 中文路径）
   - `UnicodeDecodeError` → 改用 `encoding='utf-8'` 或 `errors='replace'`
   - `TypeError` → 类型不匹配，检查参数
4. **修复代码**：用 `write_file` 修改文件
5. **验证**：`python xxx.py` 重新运行确认

注意事项：
- Windows 路径用 `\\` 或 `/`，避免中文路径
- 编码问题优先用 `encoding='utf-8', errors='replace'`
