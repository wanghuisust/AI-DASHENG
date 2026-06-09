"""Skill 系统 — Markdown 定义可复用工作流，兼容 OpenClaw ClawHub 格式

目录结构（兼容 openclaw）：
  skills/
    example-skill/
      SKILL.md          # 技能定义（frontmatter + 正文）
      references/       # 参考文档
      templates/        # 模板文件
      scripts/          # 脚本

SKILL.md 格式（兼容 openclaw frontmatter）：
  ---
  name: example-skill
  description: 示例技能描述
  triggers: [关键词1, 关键词2]
  tools: [terminal_execute, read_file]
  ---
  # 技能名称
  触发条件：...
  步骤：
  1. ...
  2. ...
  注意事项：...
"""

import json
import logging
import os
import re
import shutil
import urllib.request
import urllib.parse
from pathlib import Path

logger = logging.getLogger(__name__)

SKILLS_DIR = Path("data/skills")

# GitHub 代理（中国网络环境）
GITHUB_PROXY = os.getenv("GITHUB_RAW_PROXY", "https://gh-proxy.com/")
CLAWHUB_API = os.getenv("CLAWHUB_API", "https://api.clawhub.dev")


class Skill:
    """单个技能"""

    def __init__(self, path: Path):
        self.path = path
        self.name = path.name
        self.frontmatter = {}
        self.content = ""
        self.triggers = []
        self.tools = []
        self.description = ""
        self._linked_files = {}
        self._parse()

    def _parse(self):
        """解析 SKILL.md"""
        skill_md = self.path / "SKILL.md"
        if not skill_md.exists():
            return

        text = skill_md.read_text(encoding="utf-8")

        # 解析 frontmatter
        fm_match = re.match(r'^---\s*\n(.*?)\n---\s*\n', text, re.DOTALL)
        if fm_match:
            fm_text = fm_match.group(1)
            self.content = text[fm_match.end():]

            # 简单 YAML 解析（不引入 pyyaml 依赖）
            for line in fm_text.split("\n"):
                line = line.strip()
                if ":" in line:
                    key, val = line.split(":", 1)
                    key = key.strip()
                    val = val.strip()
                    if key == "name":
                        self.name = val
                    elif key == "description":
                        self.description = val
                    elif key == "triggers":
                        self.triggers = [t.strip().strip("'\"") for t in val.strip("[]").split(",") if t.strip()]
                    elif key == "tools":
                        self.tools = [t.strip().strip("'\"") for t in val.strip("[]").split(",") if t.strip()]
        else:
            self.content = text

        # 如果 frontmatter 没设 name，用目录名
        if not self.name:
            self.name = self.path.name

    def get_linked_file(self, rel_path: str) -> str:
        """读取关联文件（references/, templates/, scripts/）"""
        if rel_path in self._linked_files:
            return self._linked_files[rel_path]

        full_path = self.path / rel_path
        if full_path.exists() and full_path.is_file():
            try:
                content = full_path.read_text(encoding="utf-8")
                self._linked_files[rel_path] = content
                return content
            except:
                pass
        return f"(文件不存在: {rel_path})"

    def to_prompt(self) -> str:
        """转换为可注入 system prompt 的文本"""
        parts = [f"## 技能: {self.name}"]
        if self.description:
            parts.append(f"描述: {self.description}")
        parts.append(self.content)

        # 附带关联文件
        for sub in ["references", "templates", "scripts"]:
            sub_dir = self.path / sub
            if sub_dir.exists():
                for f in sorted(sub_dir.iterdir()):
                    if f.is_file() and f.suffix in (".md", ".txt", ".py", ".json", ".yaml", ".yml"):
                        content = f.read_text(encoding="utf-8", errors="replace")
                        if len(content) > 2000:
                            content = content[:2000] + "\n... (截断)"
                        parts.append(f"\n### {sub}/{f.name}\n```\n{content}\n```")

        return "\n".join(parts)


class SkillManager:
    """技能管理器 — 支持 ClawHub 安装、GitHub 下载、本地管理"""

    def __init__(self, skills_dir: str = None):
        self.skills_dir = Path(skills_dir) if skills_dir else SKILLS_DIR
        self.skills: dict[str, Skill] = {}
        self._load_all()

    def _load_all(self):
        """加载所有技能"""
        self.skills.clear()
        if not self.skills_dir.exists():
            self.skills_dir.mkdir(parents=True, exist_ok=True)
            return

        for d in sorted(self.skills_dir.iterdir()):
            if d.is_dir() and (d / "SKILL.md").exists():
                try:
                    skill = Skill(d)
                    self.skills[skill.name] = skill
                except Exception as e:
                    logger.warning(f"加载技能 {d.name} 失败: {e}")

    def reload(self):
        """重新加载所有技能（安装/删除后调用）"""
        self._load_all()
        logger.info(f"[SkillManager] 重新加载: {len(self.skills)} 个技能")

    def match(self, query: str) -> list[Skill]:
        """根据用户输入匹配相关技能"""
        query_lower = query.lower()
        matched = []
        for skill in self.skills.values():
            # 关键词匹配
            for trigger in skill.triggers:
                if trigger.lower() in query_lower:
                    matched.append(skill)
                    break
            else:
                # 名称匹配
                if skill.name.lower() in query_lower:
                    matched.append(skill)
        return matched

    def get(self, name: str) -> Skill | None:
        """按名称获取技能"""
        return self.skills.get(name)

    def list_skills(self) -> list[dict]:
        """列出所有技能摘要"""
        return [
            {"name": s.name, "description": s.description, "triggers": s.triggers}
            for s in self.skills.values()
        ]

    def create(self, name: str, description: str, content: str, triggers: list = None) -> Skill:
        """创建新技能"""
        skill_dir = self.skills_dir / name
        skill_dir.mkdir(parents=True, exist_ok=True)

        triggers = triggers or [name]
        fm = f"""---
name: {name}
description: {description}
triggers: [{', '.join(triggers)}]
---
"""
        (skill_dir / "SKILL.md").write_text(fm + content, encoding="utf-8")

        skill = Skill(skill_dir)
        self.skills[name] = skill
        return skill

    def delete(self, name: str) -> bool:
        """删除技能"""
        if name not in self.skills:
            return False
        shutil.rmtree(self.skills[name].path)
        del self.skills[name]
        return True

    def get_context_for_query(self, query: str) -> str:
        """获取与查询匹配的技能上下文，用于注入 system prompt"""
        matched = self.match(query)
        if not matched:
            return ""
        parts = ["\n\n# 匹配到的技能（优先按这些技能的步骤执行）"]
        for skill in matched:
            parts.append(skill.to_prompt())
        return "\n\n".join(parts)

    # ── 安装功能 ──────────────────────────────────────────

    def install_from_clawhub(self, skill_name: str) -> dict:
        """从 ClawHub 安装技能
        
        Returns:
            {"status": "ok"/"error", "message": str, "skill": str}
        """
        try:
            # 先检查是否已安装
            if skill_name in self.skills:
                return {"status": "error", "message": f"技能 '{skill_name}' 已安装", "skill": skill_name}

            # ClawHub API: GET /v1/skills/{name}
            url = f"{CLAWHUB_API}/v1/skills/{urllib.parse.quote(skill_name, safe='')}"
            req = urllib.request.Request(url, method="GET")
            req.add_header("Accept", "application/json")

            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            # 下载到本地
            return self._install_from_data(skill_name, data)

        except urllib.error.HTTPError as e:
            if e.code == 404:
                return {"status": "error", "message": f"ClawHub 上未找到技能 '{skill_name}'", "skill": skill_name}
            return {"status": "error", "message": f"ClawHub 请求失败 (HTTP {e.code})", "skill": skill_name}
        except Exception as e:
            return {"status": "error", "message": f"安装失败: {e}", "skill": skill_name}

    def install_from_github(self, repo_url: str, skill_path: str = "") -> dict:
        """从 GitHub 仓库安装技能
        
        Args:
            repo_url: GitHub 仓库 URL（如 https://github.com/user/repo）
            skill_path: 仓库内技能子路径（如 skills/my-skill），空则根目录
            
        Returns:
            {"status": "ok"/"error", "message": str, "skill": str}
        """
        try:
            # 解析 GitHub URL
            parsed = self._parse_github_url(repo_url, skill_path)
            if not parsed:
                return {"status": "error", "message": f"无法解析 GitHub URL: {repo_url}"}

            owner, repo, sub_path, branch = parsed

            # 先获取 SKILL.md 确认技能存在
            skill_md_url = self._github_raw_url(owner, repo, f"{sub_path}/SKILL.md", branch)
            skill_md_content = self._fetch_url(skill_md_url)
            if not skill_md_content:
                return {"status": "error", "message": f"未找到 SKILL.md: {skill_md_url}"}

            # 从 SKILL.md 解析技能名
            name = self._extract_skill_name(skill_md_content) or repo
            if name in self.skills:
                return {"status": "error", "message": f"技能 '{name}' 已安装", "skill": name}

            # 创建本地目录
            skill_dir = self.skills_dir / name
            skill_dir.mkdir(parents=True, exist_ok=True)

            # 写入 SKILL.md
            (skill_dir / "SKILL.md").write_text(skill_md_content, encoding="utf-8")

            # 下载关联文件（references/, templates/, scripts/）
            self._download_subdir(owner, repo, f"{sub_path}/references", skill_dir / "references", branch)
            self._download_subdir(owner, repo, f"{sub_path}/templates", skill_dir / "templates", branch)
            self._download_subdir(owner, repo, f"{sub_path}/scripts", skill_dir / "scripts", branch)

            # 重新加载
            self.reload()

            return {"status": "ok", "message": f"✅ 已安装技能 '{name}' (来自 GitHub)", "skill": name}

        except Exception as e:
            return {"status": "error", "message": f"GitHub 安装失败: {e}", "skill": ""}

    def search_clawhub(self, query: str, limit: int = 10) -> list[dict]:
        """搜索 ClawHub 技能"""
        try:
            url = f"{CLAWHUB_API}/v1/search?q={urllib.parse.quote(query)}&limit={limit}"
            req = urllib.request.Request(url, method="GET")
            req.add_header("Accept", "application/json")

            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            return data.get("skills", data if isinstance(data, list) else [])

        except Exception as e:
            logger.warning(f"ClawHub 搜索失败: {e}")
            return []

    # ── 内部方法 ──────────────────────────────────────────

    def _parse_github_url(self, url: str, sub_path: str = "") -> tuple | None:
        """解析 GitHub URL → (owner, repo, sub_path, branch)"""
        # https://github.com/owner/repo/tree/branch/path
        m = re.match(r'https?://github\.com/([^/]+)/([^/]+)(?:/tree/([^/]+)(?:/(.*))?)?', url)
        if m:
            owner = m.group(1)
            repo = m.group(2)
            branch = m.group(3) or "main"
            extra_path = m.group(4) or ""
            combined = f"{extra_path}/{sub_path}".strip("/") if sub_path else extra_path
            return owner, repo, combined, branch

        # 简短格式：owner/repo
        m = re.match(r'^([^/]+)/([^/]+)$', url)
        if m:
            return m.group(1), m.group(2), sub_path, "main"

        return None

    def _github_raw_url(self, owner: str, repo: str, path: str, branch: str = "main") -> str:
        """构造 GitHub raw 下载 URL（走代理）"""
        raw = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{path}"
        return GITHUB_PROXY + raw

    def _github_api_url(self, owner: str, repo: str, path: str, branch: str = "main") -> str:
        """构造 GitHub API URL"""
        return f"https://api.github.com/repos/{owner}/{repo}/contents/{path}?ref={branch}"

    def _fetch_url(self, url: str, timeout: int = 15) -> str | None:
        """下载 URL 内容"""
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except Exception as e:
            logger.warning(f"下载失败 {url}: {e}")
            return None

    def _fetch_json(self, url: str, timeout: int = 15) -> dict | list | None:
        """下载 JSON"""
        try:
            req = urllib.request.Request(url)
            req.add_header("Accept", "application/json")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            logger.warning(f"JSON 下载失败 {url}: {e}")
            return None

    def _extract_skill_name(self, skill_md_content: str) -> str | None:
        """从 SKILL.md 提取技能名"""
        fm_match = re.match(r'^---\s*\n(.*?)\n---', skill_md_content, re.DOTALL)
        if fm_match:
            for line in fm_match.group(1).split("\n"):
                if line.strip().startswith("name:"):
                    return line.split(":", 1)[1].strip().strip("'\"")
        return None

    def _download_subdir(self, owner: str, repo: str, remote_path: str, local_dir: Path, branch: str):
        """从 GitHub 下载子目录（references/templates/scripts）"""
        if not remote_path:
            return

        # 通过 GitHub API 列出目录内容
        api_url = self._github_api_url(owner, repo, remote_path, branch)
        data = self._fetch_json(api_url)
        if not data or not isinstance(data, list):
            return

        local_dir.mkdir(parents=True, exist_ok=True)
        for item in data:
            if item.get("type") == "file":
                fname = item.get("name", "")
                download_url = item.get("download_url", "")
                if not download_url:
                    # 用 raw URL 走代理
                    download_url = self._github_raw_url(owner, repo, f"{remote_path}/{fname}", branch)

                content = self._fetch_url(download_url)
                if content:
                    (local_dir / fname).write_text(content, encoding="utf-8")
                    logger.info(f"下载: {remote_path}/{fname}")

    def _install_from_data(self, skill_name: str, data: dict) -> dict:
        """从 API 数据安装技能"""
        skill_md = data.get("skill_md") or data.get("content") or data.get("readme", "")
        if not skill_md:
            # 尝试从 files 字段获取
            files = data.get("files", [])
            for f in files:
                if f.get("name") == "SKILL.md" or f.get("path", "").endswith("SKILL.md"):
                    skill_md = f.get("content", "")
                    break

        if not skill_md:
            return {"status": "error", "message": f"技能 '{skill_name}' 无 SKILL.md 内容", "skill": skill_name}

        # 创建本地目录
        skill_dir = self.skills_dir / skill_name
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(skill_md, encoding="utf-8")

        # 下载关联文件
        files = data.get("files", [])
        for f in files:
            fname = f.get("name", "")
            fpath = f.get("path", "")
            fcontent = f.get("content", "")
            if fname == "SKILL.md" or not fcontent:
                continue

            # 确定子目录
            for sub in ["references", "templates", "scripts"]:
                if sub in fpath:
                    sub_dir = skill_dir / sub
                    sub_dir.mkdir(parents=True, exist_ok=True)
                    (sub_dir / fname).write_text(fcontent, encoding="utf-8")
                    break

        self.reload()
        return {"status": "ok", "message": f"✅ 已安装技能 '{skill_name}'", "skill": skill_name}
