"""Minimal pure-Python YAML subset parser/shim.

Provides ``yaml.safe_load`` and ``yaml.safe_dump`` for the small, controlled
subset of YAML used by this project (rules packs, prompt/config files) and by
third-party libraries (e.g. huggingface-hub) that read simple YAML configs.

This shim exists because PyYAML has no cp315 wheel on PyPI and cannot be built
from source without a C compiler. It intentionally supports only a safe subset:
no anchors/aliases, no tags, no arbitrary Python objects, no executables.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Union

Scalar = Union[str, int, float, bool, None]


def _parse_scalar(text: str) -> Scalar:
    """Convert a YAML scalar string into a typed Python value."""
    if text is None:
        return None
    s = text.strip()
    if s == "":
        return ""
    # Quoted strings -> keep as-is (strip surrounding quotes).
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        return s[1:-1]
    low = s.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if low in ("null", "nil", "~", ""):
        return None
    # Integer.
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    # Float / decimal.
    if re.fullmatch(r"-?\d+\.\d+", s) or re.fullmatch(r"-?\d+\.\d+[eE][+-]?\d+", s):
        return float(s)
    # ISO-ish date/time kept as string.
    return s


def _strip_comment(line: str) -> str:
    """Remove an unquoted trailing ``#`` comment from a line."""
    in_single = in_double = False
    for i, ch in enumerate(line):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            return line[:i].rstrip()
    return line


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _parse_sequence(
    lines: List[str], index: int, base_indent: int
) -> tuple[List[Any], int]:
    """Parse a YAML sequence block at ``base_indent``."""
    result: List[Any] = []
    j = index
    while j < len(lines):
        raw = lines[j]
        if not raw.strip():
            j += 1
            continue
        if _indent(raw) < base_indent:
            break
        stripped_comment = _strip_comment(raw).rstrip()
        if stripped_comment.strip() == "":
            j += 1
            continue
        if _indent(raw) != base_indent:
            # Deeper nesting belonging to the previous sequence item.
            break
        trimmed = stripped_comment.strip()
        if not trimmed.startswith("- "):
            break
        item_text = trimmed[2:].strip()
        j += 1
        if item_text == "":
            # Nested mapping/sequence under this sequence item.
            child, j = _parse_block(lines, j, base_indent + 2)
            result.append(child)
        elif ": " in item_text:
            key, _, val = item_text.partition(": ")
            item: dict[str, Any] = {key.strip(): _parse_scalar(val)}
            # Consume deeper-indented lines that belong to this item.
            while j < len(lines) and _indent(lines[j]) > base_indent:
                sub = lines[j]
                if not sub.strip():
                    j += 1
                    continue
                scomment = _strip_comment(sub).rstrip()
                if scomment.strip() == "":
                    j += 1
                    continue
                if _indent(sub) <= base_indent:
                    break
                if ":" in scomment:
                    skey, _, sval = scomment.partition(":")
                    item[skey.strip()] = _parse_scalar(sval.strip())
                j += 1
            result.append(item)
        else:
            result.append(_parse_scalar(item_text))
    return result, j


def _parse_mapping(lines: List[str], index: int, base_indent: int) -> tuple[dict[str, Any], int]:
    """Parse a YAML mapping block at ``base_indent``."""
    result: dict[str, Any] = {}
    j = index
    while j < len(lines):
        raw = lines[j]
        if not raw.strip():
            j += 1
            continue
        if _indent(raw) < base_indent:
            break
        stripped_comment = _strip_comment(raw).rstrip()
        if stripped_comment.strip() == "":
            j += 1
            continue
        if _indent(raw) != base_indent:
            break
        if ":" not in stripped_comment:
            j += 1
            continue
        key, _, val = stripped_comment.partition(":")
        key = key.strip()
        val = val.strip()
        j += 1
        if val == "":
            # Nested block under this key.
            child, j = _parse_block(lines, j, base_indent + 2)
            result[key] = child
        else:
            result[key] = _parse_scalar(val)
    return result, j


def _parse_block(
    lines: List[str], index: int, base_indent: int
) -> tuple[Any, int]:
    """Parse a sequence of lines at ``base_indent`` into a dict or list.

    Returns (parsed_value, next_index).
    """
    # Determine whether this block is a mapping or a sequence by peeking at the
    # first content line.
    first_content = None
    for j in range(index, len(lines)):
        raw = lines[j]
        if not raw.strip():
            continue
        if _strip_comment(raw).strip() == "":
            continue
        first_content = raw
        break
    if first_content is None:
        return {}, index

    trimmed = _strip_comment(first_content).strip()
    if trimmed.startswith("- "):
        return _parse_sequence(lines, index, base_indent)
    return _parse_mapping(lines, index, base_indent)


def safe_load(text: str) -> Any:
    """Parse a YAML document (subset) into Python objects."""
    if text is None:
        return None
    # Accept a file-like object too.
    if hasattr(text, "read"):
        text = text.read()
    lines = text.split("\n")
    # Drop the document marker if present.
    if lines and lines[0].strip() == "---":
        lines.pop(0)
    value, _ = _parse_block(lines, 0, 0)
    return value


def safe_dump(data: Any) -> str:
    """Serialize basic Python objects to a small YAML subset."""
    return _dump(data, 0)


def _dump(data: Any, indent: int) -> str:
    pad = " " * indent
    if isinstance(data, dict):
        out = []
        for k, v in data.items():
            out.append(f"{pad}{k}:")
            out.append(_dump(v, indent + 2))
        return "\n".join(out)
    if isinstance(data, list):
        out = []
        for item in data:
            out.append(f"{pad}- {_dump(item, indent + 2).strip()}")
        return "\n".join(out)
    if data is None:
        return f"{pad}null"
    if isinstance(data, bool):
        return f"{pad}{'true' if data else 'false'}"
    if isinstance(data, (int, float)):
        return f"{pad}{data}"
    s = str(data)
    if any(ch in s for ch in ":#{}[]&*,?|->!@%`\n") or s.strip() != s or s == "":
        s = '"' + s.replace('"', '\\"') + '"'
    return f"{pad}{s}"


__all__ = ["safe_load", "safe_dump", "YAMLError"]


class YAMLError(Exception):
    pass
