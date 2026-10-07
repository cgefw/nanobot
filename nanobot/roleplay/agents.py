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
        raise ValueError("agent_file_unsupported")
    files: dict[str, str] = {}
    for name in AGENT_FILES:
        content = source.get(name, "")
        if not isinstance(content, str):
            raise ValueError("agent_file_not_text")
        files[name] = content
    if sum(len(content.encode("utf-8")) for content in files.values()) > MAX_PROFILE_BYTES:
        raise ValueError("agent_files_too_large")
    return files


class AgentProfile:
    def __init__(self, config: Config) -> None:
        self.name = config.roleplay.agent_name
        self.workspace = config.workspace_path.resolve()
        self.budget = max(512, config.resolve_preset().context_window_tokens // 4)

    def path(self, name: str) -> Path:
        if name not in AGENT_FILES:
            raise ValueError("agent_file_unsupported")
        path = self.workspace / name
        if path.is_symlink() or path.resolve().parent != self.workspace:
            raise ValueError("agent_file_outside_workspace")
        return path

    def files(self) -> dict[str, str]:
        files: dict[str, str] = {}
        for name in AGENT_FILES:
            path = self.path(name)
            if path.exists() and path.stat().st_size > MAX_PROFILE_BYTES:
                raise ValueError("agent_files_too_large")
            files[name] = path.read_text(encoding="utf-8") if path.exists() else ""
        return agent_files(files)

    def validate(self, files: dict[str, str]) -> None:
        content = "\n\n".join(agent_files(files).values())
        if truncate_text_to_tokens(content, self.budget) != content:
            raise ValueError("agent_prompt_over_budget")

    def preview(self) -> dict[str, Any]:
        return {"kind": "agent", "card": {"name": self.name}, "files": self.files()}

    def update(self, payload: dict[str, Any]) -> dict[str, Any]:
        name, content = payload.get("filename"), payload.get("content")
        if not isinstance(name, str) or not isinstance(content, str):
            raise ValueError("agent_file_update_invalid")
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
