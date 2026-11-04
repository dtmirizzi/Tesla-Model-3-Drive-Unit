"""Minimal, lossless-enough S-expression reader/writer for KiCad files.

Atoms are kept as `Sym` (bare symbols / numbers) or `str` (quoted strings),
so a parse -> dump round trip preserves the quoting KiCad expects.
"""

from __future__ import annotations

import re

_TOKEN = re.compile(r'\s*(?:(\()|(\))|"((?:[^"\\]|\\.)*)"|([^\s()"]+))', re.S)


class Sym(str):
    """An unquoted atom (keyword or number)."""


def _unescape(s: str) -> str:
    return s.replace('\\"', '"').replace("\\\\", "\\")


def _escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def loads(text: str) -> list:
    stack: list[list] = [[]]
    pos, n = 0, len(text)
    while pos < n:
        m = _TOKEN.match(text, pos)
        if not m:
            if text[pos:].strip() == "":
                break
            raise ValueError(f"bad token at {pos}: {text[pos:pos + 40]!r}")
        pos = m.end()
        lp, rp, qs, atom = m.groups()
        if lp:
            stack.append([])
        elif rp:
            node = stack.pop()
            stack[-1].append(node)
        elif qs is not None:
            stack[-1].append(_unescape(qs))
        elif atom is not None:
            stack[-1].append(Sym(atom))
    assert len(stack) == 1, "unbalanced parentheses"
    root = stack[0]
    return root[0] if len(root) == 1 else root


def _dump_atom(a) -> str:
    if isinstance(a, Sym):
        return str(a)
    if isinstance(a, str):
        return f'"{_escape(a)}"'
    if isinstance(a, bool):
        return "yes" if a else "no"
    if isinstance(a, float):
        s = f"{a:.6f}".rstrip("0").rstrip(".")
        return s if s not in ("-0", "") else "0"
    return str(a)


def dumps(node, indent: int = 0) -> str:
    """Pretty-print with one nested list per line (KiCad accepts any layout)."""
    if not isinstance(node, list):
        return _dump_atom(node)
    pad = "\t" * indent
    flat = all(not isinstance(c, list) for c in node)
    if flat:
        return pad + "(" + " ".join(_dump_atom(c) for c in node) + ")"
    out = [pad + "(" + " ".join(_dump_atom(c) for c in node if not isinstance(c, list))]
    # keep atoms that come after lists in order (rare, but legal)
    for c in node:
        if isinstance(c, list):
            out.append(dumps(c, indent + 1))
    out.append(pad + ")")
    return "\n".join(out)


# ---- query helpers -------------------------------------------------------


def head(node) -> str | None:
    return node[0] if isinstance(node, list) and node and isinstance(node[0], Sym) else None


def children(node, name: str) -> list[list]:
    return [c for c in node if isinstance(c, list) and head(c) == name]


def child(node, name: str) -> list | None:
    for c in node:
        if isinstance(c, list) and head(c) == name:
            return c
    return None


def value(node, name: str, default=None):
    c = child(node, name)
    return c[1] if c is not None and len(c) > 1 else default


def prop(node, name: str, default=None):
    for c in children(node, "property"):
        if len(c) > 2 and c[1] == name:
            return c[2]
    return default
