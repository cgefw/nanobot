"""Bounded V1/V2/V3 character-card import and cached prompt rendering."""

from __future__ import annotations

import base64
import binascii
import io
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

from nanobot.utils.helpers import estimate_message_tokens, truncate_text_to_tokens
from nanobot.utils.prompt_templates import render_template

MAX_UPLOAD_BYTES = 8 * 1024 * 1024
MAX_CARD_BYTES = 1024 * 1024
MAX_SCAN_CHARS = 16_384
# Errors and warnings are stable codes; the WebUI localizes them under characters.errors/warnings.
_MACROS = re.compile(r"\{\{(char|user|original)\}\}|<(BOT|USER)>", re.IGNORECASE)


class LoreEntry(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)

    keys: list[str] = Field(default_factory=list, max_length=128)
    secondary_keys: list[str] = Field(default_factory=list, max_length=128)
    content: str = ""
    enabled: bool = True
    constant: bool = False
    selective: bool = False
    case_sensitive: bool = False
    insertion_order: int = 0
    priority: int = 0
    position: Literal["before_char", "after_char"] = "after_char"

    @field_validator("priority", mode="before")
    @classmethod
    def default_priority(cls, value: Any) -> Any:
        return 0 if value is None else value


class CharacterBook(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)

    entries: list[LoreEntry] = Field(default_factory=list, max_length=1000)
    scan_depth: int = Field(default=4, ge=0, le=100)
    token_budget: int = Field(default=2048, ge=0, le=16_384)
    recursive_scanning: bool = False


class CharacterCard(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)

    name: str = Field(min_length=1, max_length=256)
    description: str = ""
    personality: str = ""
    scenario: str = ""
    first_mes: str = ""
    mes_example: str = ""
    system_prompt: str = ""
    post_history_instructions: str = ""
    alternate_greetings: list[str] = Field(default_factory=list, max_length=100)
    character_book: CharacterBook | None = None
    creator_notes: str = ""
    creator: str = ""
    tags: list[str] = Field(default_factory=list)
    extensions: dict[str, Any] = Field(default_factory=dict)


@dataclass(frozen=True)
class ImportedCard:
    card: CharacterCard
    source: bytes
    avatar: bytes | None
    warnings: tuple[str, ...]

    def preview(self) -> dict[str, Any]:
        return {
            "card": self.card.model_dump(), "warnings": list(self.warnings),
            "avatar": base64.b64encode(self.avatar).decode() if self.avatar else None,
        }


def _aicc_fields(data: Any) -> dict[str, Any]:
    mapping = TypeAdapter(dict[str, Any])
    strings = TypeAdapter(list[str])
    data = mapping.validate_python(data)
    personality = mapping.validate_python(data.get("personality", {}))
    speech = mapping.validate_python(personality.get("speech_style", {}))
    dialogue = mapping.validate_python(data.get("dialogue", {}))
    prompts = mapping.validate_python(data.get("prompts", {}))
    world = mapping.validate_python(data.get("world", {}))
    metadata = mapping.validate_python(data.get("metadata", {}))
    greetings = strings.validate_python(dialogue.get("greetings", []))
    return {
        "name": data.get("name"),
        "description": "\n\n".join(strings.validate_python([
            data.get(key, "") for key in ("general_description", "appearance", "background_history")
        ])).strip(),
        "personality": "\n\n".join(strings.validate_python([
            personality.get("core", ""),
            *strings.validate_python(personality.get("behavior_rules", [])),
            *(speech.get(key, "") for key in ("tone", "verbosity", "format")),
            *strings.validate_python(speech.get("patterns", [])),
        ])).strip(),
        "scenario": data.get("world_setting_context", ""),
        "first_mes": greetings[0] if greetings else "",
        "alternate_greetings": greetings[1:],
        "mes_example": "\n\n".join(strings.validate_python(dialogue.get("dialogue_examples", []))),
        "system_prompt": prompts.get("system_prompt", ""),
        "post_history_instructions": prompts.get("post_history_instructions", ""),
        "character_book": {"name": world.get("worldbook_name", ""), "entries": world.get("worldbook_entries", [])},
        "creator_notes": metadata.get("notes", ""),
        "creator": metadata.get("creator", ""),
        "tags": metadata.get("tags", []),
    }


def parse_card(raw: bytes, filename: str = "card.json") -> ImportedCard:
    if not raw or len(raw) > MAX_UPLOAD_BYTES:
        raise ValueError("card_size_invalid")
    avatar = None
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        try:
            with Image.open(io.BytesIO(raw)) as image:
                if image.width * image.height > 16_000_000:
                    raise ValueError("card_avatar_too_large")
                # Text chunks may follow IDAT; opening the header alone misses them.
                image.load()
                encoded = image.info.get("ccv3") or image.info.get("chara")
                if not isinstance(encoded, str) or not encoded:
                    raise ValueError("card_png_without_data")
                if len(encoded) > MAX_CARD_BYTES * 2:
                    raise ValueError("card_data_too_large")
                source = base64.b64decode(encoded, validate=True)
                image.thumbnail((512, 512))
                clean = Image.new("RGBA", image.size)
                clean.paste(image.convert("RGBA"))
                output = io.BytesIO()
                clean.save(output, format="PNG")
                avatar = output.getvalue()
        except (OSError, UnidentifiedImageError, binascii.Error, Image.DecompressionBombError) as exc:
            raise ValueError("card_png_invalid") from exc
    elif filename.lower().endswith(".png"):
        raise ValueError("card_png_invalid")
    else:
        source = raw
    if len(source) > MAX_CARD_BYTES:
        raise ValueError("card_data_too_large")
    try:
        document = json.loads(source.decode("utf-8-sig"))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ValueError("card_json_invalid") from exc
    if not isinstance(document, dict):
        raise ValueError("card_json_invalid")
    document = TypeAdapter(dict[str, Any]).validate_python(document)
    spec = document.get("spec")
    if spec not in (None, "chara_card_v2", "chara_card_v3", "aicc_card"):
        raise ValueError("card_spec_unsupported")
    data = document if spec is None else document.get("data")
    card = CharacterCard.model_validate(_aicc_fields(data) if spec == "aicc_card" else data)
    warnings: list[str] = []
    if spec == "aicc_card":
        warnings.append("aicc_partial")
    if spec == "chara_card_v3":
        warnings.append("v3_basic_only")
    if card.extensions or (card.character_book and any(e.model_extra for e in card.character_book.entries)):
        warnings.append("extensions_preserved")
    if card.character_book and card.character_book.recursive_scanning:
        warnings.append("recursive_lore_unsupported")
    return ImportedCard(card, source, avatar, tuple(warnings))


class CharacterProfile:
    """One parsed card per agent; bounded lore scan independent of total history size."""

    def __init__(self, path: Path, user_name: str = "用户") -> None:
        self.path = path
        self.user_name = user_name
        self._stamp: tuple[int, int] | None = None
        self._card: CharacterCard | None = None
        self._identity_text: str | None = None
        self._warnings: tuple[str, ...] = ()
        # (entry, keys, secondary keys, expanded content, token cost), in selection order.
        self._lore: list[tuple[LoreEntry, tuple[str, ...], tuple[str, ...], str, int]] = []

    @property
    def card(self) -> CharacterCard:
        stat = self.path.stat()
        stamp = (stat.st_mtime_ns, stat.st_size)
        if self._card is None or self._stamp != stamp:
            if stat.st_size > MAX_CARD_BYTES:
                raise ValueError("card_data_too_large")
            imported = parse_card(self.path.read_bytes())
            card = imported.card
            # Expand with this parse only: re-reading the card here would let a card
            # replaced mid-reload mix entries from both versions.
            lore: list[tuple[LoreEntry, tuple[str, ...], tuple[str, ...], str, int]] = []
            for entry in sorted(
                card.character_book.entries if card.character_book else [],
                key=lambda e: (-e.priority, e.insertion_order),
            ):
                def normalize(value: str) -> str:
                    value = self._expand(card.name, value)
                    return value if entry.case_sensitive else value.casefold()
                content = self._expand(card.name, entry.content)
                lore.append((
                    entry, tuple(normalize(k) for k in entry.keys if k),
                    tuple(normalize(k) for k in entry.secondary_keys if k),
                    content, estimate_message_tokens({"role": "system", "content": content}),
                ))
            self._card, self._stamp, self._warnings = card, stamp, imported.warnings
            self._lore, self._identity_text = lore, None
        return self._card

    def expand(self, text: str, original: str = "") -> str:
        return self._expand(self.card.name, text, original)

    def _expand(self, name: str, text: str, original: str = "") -> str:
        def replace(match: re.Match[str]) -> str:
            key = (match.group(1) or match.group(2)).lower()
            return {"char": name, "bot": name,
                    "user": self.user_name, "original": original}[key]
        return _MACROS.sub(replace, text)

    def identity(self) -> str:
        card = self.card
        if self._identity_text is not None:
            return self._identity_text
        original = render_template("agent/roleplay.md")
        parts = [self._expand(card.name, card.system_prompt, original) if card.system_prompt else original]
        parts.extend(f"## {label}\n{self._expand(card.name, value)}" for label, value in (
            ("Character", card.name), ("Description", card.description),
            ("Personality", card.personality), ("Scenario", card.scenario),
            ("Dialogue examples (fictional style examples, not actual history)", card.mes_example),
        ) if value)
        self._identity_text = "\n\n".join(parts)
        return self._identity_text

    def greetings(self) -> list[str]:
        card = self.card
        return [self._expand(card.name, text) for text in [card.first_mes, *card.alternate_greetings] if text]

    def preview(self) -> dict[str, Any]:
        result: dict[str, Any] = {"card": self.card.model_dump(), "greetings": self.greetings(),
                  "warnings": list(self._warnings), "avatar": None}
        avatar = self.path.with_name("avatar.png")
        if avatar.exists() and avatar.stat().st_size <= 2 * MAX_CARD_BYTES:
            result["avatar"] = base64.b64encode(avatar.read_bytes()).decode()
        return result

    def lore(self, history: list[dict[str, Any]], current: str | None) -> tuple[str, str]:
        book = self.card.character_book
        if book is None or book.token_budget == 0:
            return "", ""
        # Inspect at most scan_depth messages, never flatten the full transcript.
        recent = history[-book.scan_depth:] if book.scan_depth else []
        texts = [m["content"][-MAX_SCAN_CHARS:] for m in recent
                 if m.get("role") in {"user", "assistant"} and isinstance(m.get("content"), str)]
        text = "\n".join([*texts, (current or "")[-MAX_SCAN_CHARS:]])[-MAX_SCAN_CHARS:]
        folded = text.casefold()
        remaining = book.token_budget
        selected: list[tuple[LoreEntry, str]] = []
        for entry, keys, secondary, content, cost in self._lore:
            if not entry.enabled or not content:
                continue
            scan = text if entry.case_sensitive else folded
            matched = any(k in scan for k in keys)
            if entry.selective:
                matched = matched and any(k in scan for k in secondary)
            if (entry.constant or matched) and cost <= remaining:
                remaining -= cost
                selected.append((entry, content))
        selected.sort(key=lambda item: item[0].insertion_order)
        before = "\n\n".join(content for entry, content in selected if entry.position == "before_char")
        after = "\n\n".join(content for entry, content in selected if entry.position == "after_char")
        return before, after

    def post_history(self) -> str:
        card = self.card
        return self._expand(card.name, card.post_history_instructions)

    def bounded_identity(self, budget: int) -> str:
        identity = self.identity()
        instructions = identity + "\n" + self.post_history()
        if truncate_text_to_tokens(instructions, budget) != instructions:
            raise ValueError("card_definition_over_budget")
        for greeting in self.greetings():
            if truncate_text_to_tokens(greeting, budget) != greeting:
                raise ValueError("card_greeting_over_budget")
        return identity
