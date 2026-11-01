"""Markdown skill documents and immutable workspace versions."""

import os
import re
from pathlib import Path

import yaml

from .data import fingerprint

MAX_SKILL_CHARS = 16000
PROFILE = "markdown_patch_v1"
FULL_DOCUMENT_PROFILE = "markdown_v1"
MARKDOWN_PROFILES = {PROFILE, FULL_DOCUMENT_PROFILE}


def parse_document(text):
    """Validate the document envelope without prescribing its workflow or sections."""
    if len(text) > MAX_SKILL_CHARS:
        raise ValueError(f"SKILL.md exceeds {MAX_SKILL_CHARS} characters")
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise ValueError("SKILL.md must start with YAML frontmatter with name and description")
    try:
        end = lines.index("---", 1)
        metadata = yaml.safe_load("\n".join(lines[1:end]))
    except (ValueError, yaml.YAMLError) as exc:
        raise ValueError("SKILL.md has invalid YAML frontmatter") from exc
    if not isinstance(metadata, dict):
        raise ValueError("SKILL.md frontmatter must contain name and description")
    name, description = metadata.get("name"), metadata.get("description")
    if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name):
        raise ValueError("Skill name must use lowercase letters, digits and hyphens")
    if not isinstance(description, str) or not description.strip():
        raise ValueError("Skill description must explain when to use the skill")
    body = "\n".join(lines[end + 1:]).strip()
    if not body or not re.search(r"^#{1,6} \S", body, re.MULTILINE):
        raise ValueError("SKILL.md needs a Markdown body with a heading and task instructions")
    return {"name": name, "description": description}


class SkillStore:
    def __init__(self, root):
        self.root = Path(root)

    def persist(self, db_id, version):
        text = version["skill"]
        if not text:
            return
        checksum = fingerprint(text)
        # Database identifiers are external data; use a filesystem-safe namespace.
        relative = Path("skills") / fingerprint(db_id)[:16] / version["id"] / checksum / "SKILL.md"
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if path.read_bytes().decode("utf-8") != text:
                raise ValueError("已有 SKILL.md 内容与版本不符。")
        else:
            temporary = path.with_suffix(".tmp")
            temporary.write_bytes(text.encode("utf-8"))
            os.replace(temporary, path)
        version.update({"skill_path": relative.as_posix(), "skill_sha256": checksum})

    def load(self, version):
        if not version.get("skill_path"):
            return version["skill"]
        path = (self.root / version["skill_path"]).resolve()
        if not path.is_relative_to(self.root.resolve()):
            raise ValueError("Skill 路径超出工作区。")
        try:
            text = path.read_bytes().decode("utf-8")
        except OSError as exc:
            raise ValueError("版本的 SKILL.md 无法读取，请恢复对应文件。") from exc
        if fingerprint(text) != version["skill_sha256"] or text != version["skill"]:
            raise ValueError("SKILL.md 已被外部修改；请恢复版本文件，通过新候选提交修改。")
        return text
