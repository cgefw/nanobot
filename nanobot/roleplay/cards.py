"""Bounded V1/V2/V3 character-card import and cached prompt rendering."""

from __future__ import annotations

import base64
import binascii
import io
import json
import random
import re
import struct
import time
import zipfile
import zlib
from bisect import bisect_left
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, NamedTuple

import regex
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

from nanobot.utils.helpers import estimate_message_tokens, truncate_text_to_tokens
from nanobot.utils.prompt_templates import render_template

# CHARX archives carry assets beside card.json; base64 of this still fits the
# default 36 MiB WebSocket frame.
MAX_UPLOAD_BYTES = 24 * 1024 * 1024
MAX_CARD_BYTES = 1024 * 1024
MAX_SCAN_CHARS = 16_384
MAX_AVATAR_PIXELS = 16_000_000
# Lore regex keys come from untrusted cards; cap their total backtracking per turn.
REGEX_BUDGET_S = 0.25
# Errors and warnings are stable codes; the WebUI localizes them under characters.errors/warnings.
_PNG = b"\x89PNG\r\n\x1a\n"
_ZIP = b"PK\x03\x04"
# Never let PIL dispatch an untrusted icon to plugins such as EPS (Ghostscript).
_AVATAR_FORMATS = ("PNG", "JPEG", "WEBP", "GIF")
_ARCHIVE_ERRORS = (KeyError, OSError, EOFError, RuntimeError, ValueError, zipfile.BadZipFile, zlib.error)
_BRACES = re.compile(r"\{\{|\}\}")
_LEGACY_MACROS = re.compile(r"<(char|bot|user)>", re.IGNORECASE)
_MACRO_DEPTH = 16
# JavaScript-style /pattern/flags, as SillyTavern and RisuAI write regex keys.
_REGEX_KEY = re.compile(r"/(.+)/([dgimsuvy]*)", re.DOTALL)
_REGEX_FLAGS = {"i": regex.IGNORECASE, "m": regex.MULTILINE, "s": regex.DOTALL}
_SLOTS = ("before_char", "before_desc", "after_desc", "personality", "scenario", "after_char", "closing")


class LoreEntry(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)

    keys: list[str] = Field(default_factory=list, max_length=128)
    secondary_keys: list[str] = Field(default_factory=list, max_length=128)
    content: str = ""
    enabled: bool = True
    constant: bool = False
    selective: bool = False
    case_sensitive: bool = False
    use_regex: bool = False
    insertion_order: int = 0
    priority: int = 0
    position: Literal["before_char", "after_char"] = "after_char"
    extensions: dict[str, Any] = Field(default_factory=dict)

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
    extensions: dict[str, Any] = Field(default_factory=dict)


class CardAsset(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)

    type: str
    uri: str
    name: str = ""
    ext: str = ""


# Card V3: without an asset list, the container image itself is the main icon.
_DEFAULT_ASSETS = (CardAsset(type="icon", uri="ccdefault:", name="main", ext="png"),)
_LOCAL_URIS = ("ccdefault:", "embeded://", "data:")


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
    # V3; group_only_greetings, source, and dates are kept in the saved source only.
    nickname: str | None = None
    creator_notes_multilingual: dict[str, str] | None = None
    assets: list[CardAsset] | None = None

    def main_icon(self) -> CardAsset | None:
        icons = [a for a in (_DEFAULT_ASSETS if self.assets is None else self.assets) if a.type == "icon"]
        return next((a for a in icons if a.name == "main"), icons[0] if icons else None)


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


class CharacterPrompt(NamedTuple):
    definition: str  # Role instructions, character sections, and lore placed among them.
    closing: str  # High-priority lore and post-history instructions; last in the system prompt.


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


class _Decorated(NamedTuple):
    body: str
    position: str
    depth: int | None
    scan_depth: int | None
    force: bool | None
    additional: tuple[tuple[str, ...], ...]
    exclude: tuple[str, ...]
    ignored: bool


def _decorate(entry: LoreEntry, card: CharacterCard) -> _Decorated:
    """Split V3 decorators from lore content and apply the ones this runtime can honor.

    Lore is placed in the system prompt, so ``@@depth`` uses the specification's
    fallback for clients that cannot insert into the chat log. Decorators that
    need per-chat activation state or greeting/user-icon tracking are ignored,
    after trying their ``@@@`` fallbacks.
    """
    lines = entry.content.split("\n")
    groups: list[list[tuple[str, str]]] = []
    body_start = len(lines)
    for index, line in enumerate(lines):
        text = line.strip()
        if text.startswith("@@"):
            name, _, value = text.lstrip("@").partition(" ")
            if text.startswith("@@@") and groups:
                groups[-1].append((name.lower(), value.strip()))
            else:
                groups.append([(name.lower(), value.strip())])
        elif text:
            body_start = index
            break
    if not groups:
        return _Decorated(entry.content, entry.position, None, None, None, (), (), False)
    positions = {"before_desc", "after_desc"} | {
        name for name in ("personality", "scenario") if getattr(card, name)
    }
    position: str | None = None
    depth: int | None = None
    scan_depth: int | None = None
    activate = deactivate = ignored = False
    additional: list[tuple[str, ...]] = []
    exclude: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for name, value in group:
            values = tuple(item.strip() for item in value.split(",") if item.strip())
            number = int(value) if re.fullmatch(r"-?\d{1,9}", value) else None
            if name in seen and name != "additional_keys":
                pass  # Only the first decorator of a name counts.
            elif name == "activate":
                activate = True
            elif name == "dont_activate":
                deactivate = True
            elif name == "additional_keys" and values:
                additional.append(values)
            elif name == "exclude_keys" and values:
                exclude.extend(values)
            elif name == "scan_depth" and number is not None and 0 <= number <= 100:
                scan_depth = number
            elif name == "depth" and number is not None:
                depth = number
            elif name == "position" and value in positions:
                position = value
            elif name != "role" or value != "system":
                continue  # Unsupported here: try the next fallback.
            seen.add(name)
            break
        else:
            ignored = True
    return _Decorated(
        "\n".join(lines[body_start:]), position or entry.position, None if position else depth,
        scan_depth, True if activate else False if deactivate else None,
        tuple(additional), tuple(exclude), ignored,
    )


def read_card(source: bytes) -> tuple[CharacterCard, tuple[str, ...]]:
    """Validate one card JSON document; warnings depend on the document alone."""
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
        try:
            newer = float(document.get("spec_version", "3.0")) > 3.0
        except (TypeError, ValueError):
            newer = False
        if newer:
            warnings.append("v3_newer_version")
    icon = card.main_icon()
    if card.assets is not None and (
        any(asset is not icon for asset in card.assets)
        or (icon is not None and not icon.uri.startswith(_LOCAL_URIS))
    ):
        warnings.append("assets_unused")
    book = card.character_book
    if card.extensions or (book and (book.extensions or any(e.extensions for e in book.entries))):
        warnings.append("extensions_preserved")
    if book and book.recursive_scanning:
        warnings.append("recursive_lore_unsupported")
    if book and any(_decorate(entry, card).ignored for entry in book.entries):
        warnings.append("lore_decorators_ignored")
    return card, tuple(warnings)


def _png_card_text(raw: bytes) -> bytes:
    """Return the decoded ccv3 (preferred) or chara tEXt payload.

    Walk the chunks directly so data written after APNG frames is still found.
    """
    found: dict[bytes, bytes] = {}
    position = len(_PNG)
    while position + 12 <= len(raw):
        length, kind = struct.unpack_from(">I4s", raw, position)
        end = position + 8 + length
        if end + 4 > len(raw):
            raise ValueError("card_png_invalid")
        if kind == b"IEND":
            break
        if kind == b"tEXt":
            keyword, _, text = raw[position + 8:end].partition(b"\0")
            if keyword in (b"ccv3", b"chara"):
                found.setdefault(keyword, text)
        position = end + 4
    encoded = found.get(b"ccv3") or found.get(b"chara")
    if not encoded:
        raise ValueError("card_png_without_data")
    if len(encoded) > MAX_CARD_BYTES * 2:
        raise ValueError("card_data_too_large")
    try:
        return base64.b64decode(encoded, validate=True)
    except binascii.Error as exc:
        raise ValueError("card_png_invalid") from exc


def _archive_member(archive: zipfile.ZipFile, name: str, limit: int) -> bytes:
    info = archive.getinfo(name)
    if info.file_size > limit:
        raise ValueError("card_data_too_large")
    with archive.open(info) as member:
        # Bound the read as well: a forged header must not inflate past the limit.
        data = member.read(limit + 1)
    if len(data) > limit:
        raise ValueError("card_data_too_large")
    return data


def _avatar(data: bytes) -> bytes:
    """Return a metadata-free PNG thumbnail of at most 512 pixels."""
    try:
        with Image.open(io.BytesIO(data), formats=_AVATAR_FORMATS) as image:
            if image.width * image.height > MAX_AVATAR_PIXELS:
                raise ValueError("card_avatar_too_large")
            image.thumbnail((512, 512))
            clean = Image.new("RGBA", image.size)
            clean.paste(image.convert("RGBA"))
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
        raise ValueError("card_png_invalid") from exc
    output = io.BytesIO()
    clean.save(output, format="PNG")
    return output.getvalue()


def _icon(uri: str, archive: zipfile.ZipFile | None) -> bytes | None:
    """Read an embedded or inline icon; remote URLs are never fetched on import."""
    if uri.startswith("embeded://") and archive is not None:
        return _archive_member(archive, uri.removeprefix("embeded://"), MAX_UPLOAD_BYTES)
    header, comma, data = uri.partition(",")
    if comma and header.startswith("data:") and header.endswith(";base64"):
        return base64.b64decode(data, validate=True)
    return None


def parse_card(raw: bytes, filename: str = "card.json") -> ImportedCard:
    """Import a JSON, PNG/APNG, or CHARX card and its main icon."""
    if not raw or len(raw) > MAX_UPLOAD_BYTES:
        raise ValueError("card_size_invalid")
    archive = None
    if raw.startswith(_PNG):
        source = _png_card_text(raw)
    elif raw.startswith(_ZIP):
        try:
            archive = zipfile.ZipFile(io.BytesIO(raw))
            source = _archive_member(archive, "card.json", MAX_CARD_BYTES)
        except KeyError as exc:
            raise ValueError("card_charx_without_data") from exc
        except (OSError, EOFError, RuntimeError, zipfile.BadZipFile, zlib.error) as exc:
            raise ValueError("card_charx_invalid") from exc
    elif filename.lower().endswith(".png"):
        raise ValueError("card_png_invalid")
    elif filename.lower().endswith(".charx"):
        raise ValueError("card_charx_invalid")
    else:
        source = raw
    card, warnings = read_card(source)
    icon = card.main_icon()
    avatar = None
    if icon is not None and icon.uri != "ccdefault:":
        try:
            data = _icon(icon.uri, archive)
            avatar = _avatar(data) if data else None
        except _ARCHIVE_ERRORS:
            avatar = None
        if avatar is None and "assets_unused" not in warnings:
            warnings += ("assets_unused",)
    if avatar is None and archive is None and raw.startswith(_PNG):
        # The card image stands in for a missing or unreadable main icon.
        avatar = _avatar(raw)
    return ImportedCard(card, source, avatar, warnings)


class _Macros:
    """Curly-braced syntax for one card load.

    Random values are drawn once per load, so a greeting preview, the saved
    greeting, and the cached system prompt agree until the card changes.
    """

    def __init__(self, char: str, user: str) -> None:
        self.char = char
        self.user = user
        self.rng = random.Random()

    def __call__(self, text: str, original: str = "") -> str:
        stack: list[int] = []
        pairs: dict[int, int] = {}
        for match in _BRACES.finditer(text):
            if match.group() == "{{":
                stack.append(match.start())
            elif stack:
                pairs[stack.pop()] = match.end()
        opens = sorted(pairs)

        def legacy(segment: str) -> str:
            return _LEGACY_MACROS.sub(
                lambda m: self.user if m.group(1).lower() == "user" else self.char, segment,
            )

        def walk(low: int, high: int, depth: int) -> str:
            parts: list[str] = []
            index = bisect_left(opens, low)
            while index < len(opens) and opens[index] < high:
                start, end = opens[index], pairs[opens[index]]
                parts.append(legacy(text[low:start]))
                inner = walk(start + 2, end - 2, depth + 1) if depth < _MACRO_DEPTH else text[start + 2:end - 2]
                value = self._macro(inner, original, f"{start}:{inner}")
                parts.append("{{" + inner + "}}" if value is None else value)
                low = end
                index = bisect_left(opens, end, index)
            parts.append(legacy(text[low:high]))
            return "".join(parts)

        return walk(0, len(text), 0)

    def _macro(self, body: str, original: str, seed: str) -> str | None:
        if body.lstrip().startswith("//"):
            return ""
        name, separator, argument = body.partition(":")
        name = name.strip().lower()
        if not separator:
            return {"char": self.char, "user": self.user, "original": original}.get(name)
        if name in ("hidden_key", "comment"):
            return ""
        if name == "reverse":
            return argument[::-1]
        if name in ("random", "pick"):
            choices = argument[1:].split("::") if argument.startswith(":") else [
                choice.replace("\\,", ",") for choice in re.split(r"(?<!\\),", argument)
            ]
            # pick stays the same for the same prompt text.
            return (random.Random(seed) if name == "pick" else self.rng).choice(choices)
        if name == "roll":
            sides = argument.strip().lower().removeprefix("d")
            if sides.isascii() and sides.isdigit() and int(sides) > 0:
                return str(self.rng.randint(1, int(sides)))
        return None


_Key = str | regex.Pattern[str]


@dataclass(frozen=True)
class _Lore:
    entry: LoreEntry
    content: str
    cost: int
    keys: tuple[_Key, ...]
    secondary: tuple[_Key, ...]
    additional: tuple[tuple[_Key, ...], ...]
    exclude: tuple[_Key, ...]
    position: str
    depth: int | None
    scan_depth: int | None
    force: bool | None


@dataclass(frozen=True)
class _Loaded:
    card: CharacterCard
    warnings: tuple[str, ...]
    # Expanded role instructions, character, description, personality, scenario, examples.
    sections: tuple[str, str, str, str, str, str]
    greetings: tuple[str, ...]
    post_history: str
    lore: tuple[_Lore, ...]  # Selection order: highest priority first.


def _key(value: str, entry: LoreEntry) -> _Key | None:
    """Compile one lore key; with use_regex, ``/pattern/flags`` keys are regular expressions.

    SillyTavern exports every entry with use_regex and plain keys, so keys
    without slashes stay plain substrings. Invalid patterns never match.
    """
    if not value:
        return None
    if entry.use_regex and (match := _REGEX_KEY.fullmatch(value)):
        flags = 0
        for flag in match.group(2):
            flags |= _REGEX_FLAGS.get(flag, 0)
        try:
            return regex.compile(match.group(1), flags)
        except (regex.error, RecursionError, OverflowError):
            return None
    return value if entry.case_sensitive else value.casefold()


class CharacterProfile:
    """One parsed card per agent; bounded lore scan independent of total history size."""

    def __init__(self, path: Path, user_name: str = "用户") -> None:
        self.path = path
        self.user_name = user_name
        self._stamp: tuple[int, int] | None = None
        self._state: _Loaded | None = None

    def _load(self) -> _Loaded:
        stat = self.path.stat()
        stamp = (stat.st_mtime_ns, stat.st_size)
        if self._state is None or self._stamp != stamp:
            if stat.st_size > MAX_CARD_BYTES:
                raise ValueError("card_data_too_large")
            # Build everything from this one read and publish it together:
            # a card replaced mid-reload must not mix entries from both versions.
            self._state, self._stamp = self._compile(*read_card(self.path.read_bytes())), stamp
        return self._state

    def _compile(self, card: CharacterCard, warnings: tuple[str, ...]) -> _Loaded:
        macros = _Macros(card.nickname or card.name, self.user_name)
        original = render_template("agent/roleplay.md")
        character = macros(card.name)
        if card.nickname and card.nickname != card.name:
            character += f"\nNickname: {macros(card.nickname)}"
        lore: list[_Lore] = []
        book = card.character_book
        for entry in sorted(book.entries if book else [], key=lambda e: (-e.priority, e.insertion_order)):
            decorated = _decorate(entry, card)

            def keys(values: tuple[str, ...] | list[str], entry: LoreEntry = entry) -> tuple[_Key, ...]:
                return tuple(key for value in values if (key := _key(macros(value), entry)) is not None)

            content = macros(decorated.body)
            lore.append(_Lore(
                entry, content, estimate_message_tokens({"role": "system", "content": content}),
                keys(entry.keys), keys(entry.secondary_keys),
                tuple(keys(group) for group in decorated.additional), keys(decorated.exclude),
                decorated.position, decorated.depth, decorated.scan_depth, decorated.force,
            ))
        return _Loaded(
            card, warnings,
            (macros(card.system_prompt, original) if card.system_prompt else original, character,
             macros(card.description), macros(card.personality), macros(card.scenario),
             macros(card.mes_example)),
            tuple(macros(text) for text in [card.first_mes, *card.alternate_greetings] if text),
            macros(card.post_history_instructions),
            tuple(lore),
        )

    @property
    def card(self) -> CharacterCard:
        return self._load().card

    @staticmethod
    def _definition(state: _Loaded, slots: dict[str, list[str]]) -> str:
        head, character, description, personality, scenario, examples = state.sections
        parts = [head, f"## Character\n{character}", *slots["before_desc"]]
        if description:
            parts.append(f"## Description\n{description}")
        parts.extend(slots["after_desc"])
        for label, value, extra in (("Personality", personality, slots["personality"]),
                                    ("Scenario", scenario, slots["scenario"])):
            if value:
                parts.append(f"## {label}\n" + "\n\n".join([value, *extra]))
        if examples:
            parts.append(f"## Dialogue examples (fictional style examples, not actual history)\n{examples}")
        return "\n\n---\n\n".join(filter(None, (
            "\n\n".join(slots["before_char"]), "\n\n".join(parts), "\n\n".join(slots["after_char"]),
        )))

    def identity(self) -> str:
        """The fixed character definition without lore."""
        return self._definition(self._load(), {slot: [] for slot in _SLOTS})

    def greetings(self) -> list[str]:
        return list(self._load().greetings)

    def post_history(self) -> str:
        return self._load().post_history

    def preview(self) -> dict[str, Any]:
        state = self._load()
        result: dict[str, Any] = {"card": state.card.model_dump(), "greetings": list(state.greetings),
                                  "warnings": list(state.warnings), "avatar": None}
        avatar = self.path.with_name("avatar.png")
        if avatar.exists() and avatar.stat().st_size <= 2 * MAX_CARD_BYTES:
            result["avatar"] = base64.b64encode(avatar.read_bytes()).decode()
        return result

    def prompt(self, history: list[dict[str, Any]], current: str | None) -> CharacterPrompt:
        """Render the definition with lore matched against recent messages."""
        state = self._load()
        book = state.card.character_book
        slots: dict[str, list[str]] = {slot: [] for slot in _SLOTS}
        if book is not None and book.token_budget:
            scans: dict[int, tuple[str, str]] = {}
            deadline = time.monotonic() + REGEX_BUDGET_S

            def scan(depth: int) -> tuple[str, str]:
                # Inspect at most scan_depth messages, never flatten the full transcript.
                if depth not in scans:
                    recent = history[-depth:] if depth else []
                    texts = [m["content"][-MAX_SCAN_CHARS:] for m in recent
                             if m.get("role") in {"user", "assistant"} and isinstance(m.get("content"), str)]
                    text = "\n".join([*texts, (current or "")[-MAX_SCAN_CHARS:]])[-MAX_SCAN_CHARS:]
                    scans[depth] = (text, text.casefold())
                return scans[depth]

            def hit(keys: tuple[_Key, ...], text: tuple[str, str], case_sensitive: bool) -> bool:
                for key in keys:
                    if isinstance(key, str):
                        if key in text[0 if case_sensitive else 1]:
                            return True
                        continue
                    timeout = deadline - time.monotonic()
                    try:
                        if timeout > 0 and key.search(text[0], timeout=timeout):
                            return True
                    except TimeoutError:
                        continue
                return False

            remaining = book.token_budget
            selected: list[_Lore] = []
            for lore in state.lore:
                entry = lore.entry
                if not entry.enabled or not lore.content:
                    continue
                matched = lore.force
                if matched is None:
                    text = scan(book.scan_depth if lore.scan_depth is None else lore.scan_depth)
                    sensitive = entry.case_sensitive
                    matched = (
                        entry.constant or (
                            hit(lore.keys, text, sensitive)
                            # Selective entries without secondary keys match on keys alone.
                            and not (entry.selective and lore.secondary and not hit(lore.secondary, text, sensitive))
                        )
                    ) and all(hit(group, text, sensitive) for group in lore.additional) and not hit(
                        lore.exclude, text, sensitive,
                    )
                if matched and lore.cost <= remaining:
                    remaining -= lore.cost
                    selected.append(lore)
            messages = sum(m.get("role") in {"user", "assistant"} for m in history) + (current is not None)
            for lore in sorted(selected, key=lambda item: item.entry.insertion_order):
                # Without chat-log insertion, the V3 fallback for @@depth: recent
                # depths take the high-priority end, older ones follow the character.
                slot = lore.position if lore.depth is None else (
                    "closing" if lore.depth <= messages else "after_char"
                )
                slots[slot].append(lore.content)
        closing = "\n\n".join(filter(None, [*slots["closing"], state.post_history]))
        return CharacterPrompt(self._definition(state, slots), closing)

    def bounded_identity(self, budget: int) -> str:
        identity = self.identity()
        instructions = identity + "\n" + self.post_history()
        if truncate_text_to_tokens(instructions, budget) != instructions:
            raise ValueError("card_definition_over_budget")
        for greeting in self.greetings():
            if truncate_text_to_tokens(greeting, budget) != greeting:
                raise ValueError("card_greeting_over_budget")
        return identity
