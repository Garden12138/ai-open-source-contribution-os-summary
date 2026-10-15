"""Context compression utilities for large repository planning.

Provides AST-aware and heuristic skeletonization for Python, TypeScript/JavaScript,
and general text files. Inspired by Cursor and Codex context-budgeting models.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import PurePosixPath
from typing import Any

# File extensions for binary/media/packaging noise that should not pollute code inventory
NOISY_EXTENSIONS = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".svg",
        ".webp",
        ".ico",
        ".mp3",
        ".mp4",
        ".wav",
        ".ogg",
        ".woff",
        ".woff2",
        ".ttf",
        ".eot",
        ".otf",
        ".zip",
        ".tar",
        ".gz",
        ".tgz",
        ".bz2",
        ".xz",
        ".7z",
        ".pdf",
        ".exe",
        ".dll",
        ".so",
        ".dylib",
        ".bin",
        ".pyc",
        ".pyo",
        ".pyd",
        ".class",
    }
)

DEFAULT_COMPRESSION_THRESHOLD_BYTES = 15_000


def is_noisy_path(path: str) -> bool:
    """Return True if path points to a binary/media/build-cache asset."""
    ext = PurePosixPath(path).suffix.lower()
    return ext in NOISY_EXTENSIONS


def compress_python(code: str) -> str:
    """Extract class/function signatures and docstrings, replacing bodies with ellipsis."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return compress_generic_text(code)

    class Skeletonizer(ast.NodeTransformer):
        def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.FunctionDef:
            return self._skeletonize_func(node)

        def visit_AsyncFunctionDef(
            self, node: ast.AsyncFunctionDef
        ) -> ast.AsyncFunctionDef:
            return self._skeletonize_func(node)

        def _skeletonize_func(self, node: Any) -> Any:
            docstring = ast.get_docstring(node)
            new_body: list[ast.stmt] = []
            if docstring:
                short_doc = "\n".join(docstring.strip().splitlines()[:2])
                new_body.append(ast.Expr(value=ast.Constant(value=short_doc)))
            new_body.append(ast.Expr(value=ast.Constant(value=Ellipsis)))
            node.body = new_body
            return node

        def visit_ClassDef(self, node: ast.ClassDef) -> ast.ClassDef:
            docstring = ast.get_docstring(node)
            self.generic_visit(node)
            if docstring and node.body:
                first = node.body[0]
                if (
                    isinstance(first, ast.Expr)
                    and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)
                ):
                    short_doc = "\n".join(docstring.strip().splitlines()[:2])
                    first.value.value = short_doc
            return node

    skeleton = Skeletonizer().visit(tree)
    ast.fix_missing_locations(skeleton)
    try:
        return ast.unparse(skeleton)
    except Exception:
        return compress_generic_text(code)


_TS_METHOD_START = re.compile(
    r"^\s*(?:export\s+)?(?:public\s+|private\s+|protected\s+|static\s+|readonly\s+|async\s+)*"
    r"(?:function\s+\w+|\w+)\s*\(",
    re.MULTILINE,
)

_TS_EXCLUDE_KEYWORDS = re.compile(
    r"\b(?:if|for|while|switch|catch|do|else|return)\b"
)


def compress_typescript_or_javascript(code: str) -> str:
    """Collapse method/function implementations in TS/JS files, retaining signatures and interfaces."""
    lines = code.splitlines()
    output: list[str] = []
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i]
        stripped = line.strip()

        # Check for start of method/function signature
        is_candidate = bool(_TS_METHOD_START.search(line)) and not bool(
            _TS_EXCLUDE_KEYWORDS.search(line)
        )
        if "class " in line or "interface " in line or "enum " in line or "type " in line:
            is_candidate = False

        if is_candidate:
            sig_lines = []
            brace_found = False
            start_i = i

            while i < n and not brace_found and (i - start_i) < 15:
                sig_lines.append(lines[i])
                if "{" in lines[i]:
                    brace_found = True
                i += 1

            if brace_found:
                last_sig = sig_lines[-1]
                before_brace = last_sig.split("{")[0].rstrip()
                sig_lines[-1] = before_brace + " { /* ... */ }"
                output.extend(sig_lines)

                # Skip until matching closing brace
                brace_count = 1 + last_sig.count("{") - 1 - last_sig.count("}")
                omitted = 0
                while i < n and brace_count > 0:
                    brace_count += lines[i].count("{") - lines[i].count("}")
                    omitted += 1
                    i += 1
                continue
            else:
                # Did not find brace; revert
                i = start_i

        output.append(line)
        i += 1

    result = "\n".join(output)
    # If compression barely made a difference or file is still huge, run line-based fold
    if len(result.encode()) > 40_000:
        return compress_generic_text(result, max_lines=300)
    return result


def compress_generic_text(code: str, max_lines: int = 150) -> str:
    """Fallback compression for large text or data files."""
    lines = code.splitlines()
    if len(lines) <= max_lines:
        return code

    head_count = max(20, max_lines // 2)
    tail_count = max(15, max_lines // 4)
    omitted = len(lines) - head_count - tail_count

    head = lines[:head_count]
    tail = lines[-tail_count:] if tail_count > 0 else []
    return (
        "\n".join(head)
        + f"\n\n// ... [omitted {omitted} lines for context budget] ...\n\n"
        + "\n".join(tail)
    )


def compress_file(
    path: str,
    content: str,
    threshold_bytes: int = DEFAULT_COMPRESSION_THRESHOLD_BYTES,
) -> str:
    """Compress a single file content if its size exceeds the threshold."""
    if len(content.encode("utf-8")) <= threshold_bytes:
        return content

    ext = PurePosixPath(path).suffix.lower()
    if ext in {".py", ".pyi"}:
        return compress_python(content)
    if ext in {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}:
        return compress_typescript_or_javascript(content)
    return compress_generic_text(content)


def compress_evidence_for_prompt(
    evidence: dict[str, Any],
    *,
    focus_paths: set[str] = frozenset(),
    threshold_bytes: int = DEFAULT_COMPRESSION_THRESHOLD_BYTES,
) -> dict[str, Any]:
    """Return a compressed view of evidence specifically formatted for the LLM prompt.

    Preserves full text for focus_paths, and produces AST/signature skeletons
    for large utility or background files.
    """
    raw_inventory = evidence.get("inventory", [])
    filtered_inventory = [
        p for p in raw_inventory if not is_noisy_path(p)
    ]

    compressed_entries = []
    for entry in evidence.get("entries", []):
        path = entry.get("path", "")
        if path not in focus_paths and any(part in {"i18n", "locales", "translations"} for part in PurePosixPath(path).parts):
            # Keep paths discoverable in inventory; omit unrelated translations
            # instead of resending dozens of copies of the same README.
            continue
        content = entry.get("content")
        if content is None or path in focus_paths:
            compressed_entries.append(dict(entry))
            continue

        raw_bytes = len(content.encode("utf-8"))
        if raw_bytes > threshold_bytes:
            skeleton = compress_file(path, content, threshold_bytes=threshold_bytes)
            entry_copy = dict(entry)
            entry_copy["content"] = skeleton
            entry_copy["skeleton"] = True
            compressed_entries.append(entry_copy)
        else:
            compressed_entries.append(dict(entry))

    return {
        "inventory": filtered_inventory,
        "entries": compressed_entries,
        "truncated": evidence.get("truncated", False),
    }
