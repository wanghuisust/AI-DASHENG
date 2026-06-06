"""Skill 系统 — Markdown 定义可复用工作流，类似 Hermes Agent 的 Skill

目录结构：
  skills/
    example-skill/
      SKILL.md          # 技能定义（frontmatter + 正文）
      references/       # 参考文档
      templates/        # 模板文件
      scripts/          # 脚本

SKILL.md 格式：
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

import os
import re
import json
from pathlib import Path

SKILLS_DIR = Path("data/skills")


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
                        # [a, b, c] 格式
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
    """技能管理器"""

    def __init__(self, skills_dir: str = None):
        self.skills_dir = Path(skills_dir) if skills_dir else SKILLS_DIR
        self.skills: dict[str, Skill] = {}
        self._load_all()

    def _load_all(self):
        """加载所有技能"""
        if not self.skills_dir.exists():
            self.skills_dir.mkdir(parents=True, exist_ok=True)
            return

        for d in sorted(self.skills_dir.iterdir()):
            if d.is_dir() and (d / "SKILL.md").exists():
                try:
                    skill = Skill(d)
                    self.skills[skill.name] = skill
                except Exception as e:
                    print(f"Warning: 加载技能 {d.name} 失败: {e}")

    def match(self, query: str) -> list[Skill]:
        """根据用户输入匹配相关技能"""
        query_lower = query.lower()
        matched = []
        for skill in self.skills.values():
            for trigger in skill.triggers:
                if trigger.lower() in query_lower:
                    matched.append(skill)
                    break
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
        import shutil
        shutil.rmtree(self.skills[name].path)
        del self.skills[name]
        return True

    def get_context_for_query(self, query: str) -> str:
        """获取与查询匹配的技能上下文，用于注入 system prompt"""
        matched = self.match(query)
        if not matched:
            return ""
        parts = ["\n\n# 匹配到的技能"]
        for skill in matched:
            parts.append(skill.to_prompt())
        return "\n\n".join(parts)
