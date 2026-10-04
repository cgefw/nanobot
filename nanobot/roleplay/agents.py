"""Editable native agent instructions, scoped to the agent's own workspace."""

from pathlib import Path
from typing import Any, cast

from nanobot.config.schema import Config
from nanobot.utils.helpers import (
    _write_text_atomic,  # pyright: ignore[reportPrivateUsage]
    load_bundled_template,
    truncate_text_to_tokens,
)

AGENT_FILES = ("AGENTS.md", "SOUL.md", "USER.md")
MAX_PROFILE_BYTES = 1024 * 1024


def agent_files(value: object) -> dict[str, str]:
    source = cast(dict[object, object], value) if isinstance(value, dict) else None
    if source is None or set(source) - set(AGENT_FILES):
        raise ValueError("仅支持 AGENTS.md、SOUL.md 和 USER.md")
    files: dict[str, str] = {}
    for name in AGENT_FILES:
        content = source.get(name, "")
        if not isinstance(content, str):
            raise ValueError(f"{name} 必须是文本")
        files[name] = content
    if sum(len(content.encode("utf-8")) for content in files.values()) > MAX_PROFILE_BYTES:
        raise ValueError("Agent 提示词文件总大小不能超过 1 MiB")
    return files


class AgentProfile:
    def __init__(self, config: Config) -> None:
        self.name = config.roleplay.agent_name
        self.workspace = config.workspace_path.resolve()
        self.budget = max(512, config.resolve_preset().context_window_tokens // 4)

    def path(self, name: str) -> Path:
        if name not in AGENT_FILES:
            raise ValueError("不支持的 Agent 提示词文件")
        path = self.workspace / name
        if path.is_symlink() or path.resolve().parent != self.workspace:
            raise ValueError("Agent 提示词文件必须位于当前工作区内")
        return path

    def files(self) -> dict[str, str]:
        files: dict[str, str] = {}
        for name in AGENT_FILES:
            path = self.path(name)
            if path.exists() and path.stat().st_size > MAX_PROFILE_BYTES:
                raise ValueError(f"{name} 超过大小限制")
            files[name] = path.read_text(encoding="utf-8") if path.exists() else ""
        return agent_files(files)

    def validate(self, files: dict[str, str]) -> None:
        content = "\n\n".join(agent_files(files).values())
        if truncate_text_to_tokens(content, self.budget) != content:
            raise ValueError("Agent 提示词超过当前模型的上下文预算，请缩短内容")

    def preview(self) -> dict[str, Any]:
        return {"kind": "agent", "card": {"name": self.name}, "files": self.files()}

    def update(self, payload: dict[str, Any]) -> dict[str, Any]:
        name, content = payload.get("filename"), payload.get("content")
        if not isinstance(name, str) or not isinstance(content, str):
            raise ValueError("请指定文件名和文本内容")
        path = self.path(name)
        files = self.files()
        files[name] = content
        self.validate(files)
        _write_text_atomic(path, content)
        return self.preview()

    @staticmethod
    def defaults(files: dict[str, str]) -> dict[str, str]:
        return {name: content if content.strip() else load_bundled_template(name) or ""
                for name, content in files.items()}
