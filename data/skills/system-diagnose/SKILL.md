---
name: system-diagnose
description: 诊断系统问题（网络、磁盘、进程、GPU）
triggers: [诊断, 排查, 系统问题, 卡顿, 崩溃, 蓝屏, 慢, 故障]
tools: [terminal_execute]
---

# 系统诊断

当用户报告系统问题时，按以下步骤排查：

1. **系统概览**：`systeminfo | findstr /C:"OS" /C:"Memory"`（Windows）或 `uname -a && free -h`（Linux）
2. **磁盘空间**：`df -h`（Linux）或 `wmic logicaldisk get size,freespace,caption`（Windows）
3. **内存占用**：查看最占内存的进程
4. **GPU 状态**：`nvidia-smi`（如有 NVIDIA 显卡）
5. **网络连通**：`ping -c 3 baidu.com`

注意事项：
- Windows 用 `tasklist /FI "MEMUSAGE gt 100000"` 找大内存进程
- Linux 用 `ps aux --sort=-%mem | head -10`
- GPU 显存满时不一定是 GPU 占用高，可能是残留进程
