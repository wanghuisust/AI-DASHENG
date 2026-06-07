"""临时文件管理 — 统一的临时缓冲目录

所有 AI Agent 执行任务时生成的临时脚本文件都放在这里，
任务完成后自动清理。
"""

import os
import shutil
import time
import threading
from pathlib import Path


# 临时目录路径（data/.cache/tmp/）
def get_tmp_dir():
    """获取临时目录路径"""
    tmp_dir = Path(__file__).resolve().parent.parent.parent / "data" / ".cache" / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    return tmp_dir


def get_tmp_file(suffix: str = ".tmp") -> str:
    """创建一个唯一的临时文件路径
    
    Args:
        suffix: 文件后缀，默认 .tmp
        
    Returns:
        临时文件的完整路径
    """
    tmp_dir = get_tmp_dir()
    filename = f"tmp_{int(time.time())}_{os.getpid()}_{id(object())}{suffix}"
    return str(tmp_dir / filename)


def cleanup_tmp_dir(max_age_hours: int = 24):
    """清理临时目录中超过指定时间的文件
    
    Args:
        max_age_hours: 最大保留小时数，默认24小时
    """
    tmp_dir = get_tmp_dir()
    if not tmp_dir.exists():
        return
    
    now = time.time()
    max_age_seconds = max_age_hours * 3600
    cleaned = 0
    
    for f in tmp_dir.iterdir():
        if f.is_file() and (now - f.stat().st_mtime) > max_age_seconds:
            try:
                f.unlink()
                cleaned += 1
            except Exception:
                pass
    
    if cleaned > 0:
        print(f"[TMP] Cleaned {cleaned} expired temp files from {tmp_dir}")


def cleanup_tmp_files(files: list):
    """清理指定的临时文件列表
    
    Args:
        files: 文件路径列表
    """
    for f in files:
        try:
            if os.path.exists(f):
                os.unlink(f)
        except Exception:
            pass


def get_tmp_dir_path() -> str:
    """获取临时目录路径字符串"""
    return str(get_tmp_dir())
