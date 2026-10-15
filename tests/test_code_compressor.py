from app.code_compressor import (
    compress_python,
    compress_typescript_or_javascript,
    compress_generic_text,
    compress_file,
    compress_evidence_for_prompt,
    is_noisy_path,
)


def test_is_noisy_path():
    assert is_noisy_path("assets/image.png") is True
    assert is_noisy_path("fonts/font.woff2") is True
    assert is_noisy_path("app/main.py") is False
    assert is_noisy_path("src/index.ts") is False


def test_compress_python():
    code = '''"""Module docstring."""

import os

class MyService:
    """Service class docstring."""

    def __init__(self, name: str):
        self.name = name
        self.items = []
        for i in range(100):
            self.items.append(i * 2)

    async def fetch_data(self, query: str) -> list[str]:
        """Fetch data from remote.

        Detailed explanation.
        """
        results = []
        if query:
            results.append(query)
        return results
'''
    res = compress_python(code)
    assert "class MyService:" in res
    assert "def __init__(self, name: str):" in res
    assert "async def fetch_data(self, query: str) -> list[str]:" in res
    assert "for i in range(100):" not in res
    assert "if query:" not in res
    assert "..." in res


def test_compress_typescript():
    ts_code = """import { Component } from '@angular/core';

export class LoggedInUser {
  readonly id: string;

  constructor(id: string) {
    this.id = id;
    console.log('init user');
  }

  async navigateTo(url: string): Promise<void> {
    const full = 'http://localhost' + url;
    await page.goto(full);
    await page.waitForLoadState();
  }

  get isReady(): boolean {
    return true;
  }
}
"""
    res = compress_typescript_or_javascript(ts_code)
    assert "export class LoggedInUser" in res
    assert "readonly id: string;" in res
    assert "async navigateTo(url: string): Promise<void> { /* ... */ }" in res
    assert "await page.goto" not in res


def test_compress_generic_text():
    lines = [f"line {i}" for i in range(200)]
    text = "\n".join(lines)
    res = compress_generic_text(text, max_lines=40)
    assert len(res.splitlines()) < 100
    assert "omitted" in res


def test_compress_evidence_for_prompt():
    evidence = {
        "inventory": ["app/main.py", "assets/logo.png", "src/helper.ts"],
        "entries": [
            {
                "path": "app/main.py",
                "prior_hash": "hash1",
                "content": "def run():\n    pass\n" * 500,
                "executable": False,
            },
            {
                "path": "src/helper.ts",
                "prior_hash": "hash2",
                "content": "export function help() {\n    return 42;\n}\n" * 500,
                "executable": False,
            },
        ],
        "truncated": False,
    }
    # If app/main.py is a focus path, it shouldn't be skeletonized
    compressed = compress_evidence_for_prompt(
        evidence, focus_paths={"app/main.py"}, threshold_bytes=500
    )
    # logo.png should be pruned from inventory
    assert "assets/logo.png" not in compressed["inventory"]
    assert "app/main.py" in compressed["inventory"]

    entries_by_path = {e["path"]: e for e in compressed["entries"]}
    assert entries_by_path["app/main.py"].get("skeleton") is not True
    assert entries_by_path["src/helper.ts"].get("skeleton") is True


def test_explicit_large_source_preserves_original_docstring_and_hash():
    import hashlib
    source = "def directory():\n    \"\"\"Summary.\n\n" + "    Documentation line.\n" * 17000 + "    will crete the file\n    \"\"\"\n    return True\n"
    entry = {"path": "salt/states/file.py", "content": source, "executable": False,
             "prior_hash": hashlib.sha256(source.encode()).hexdigest()}
    evidence = {"inventory": [entry["path"]], "entries": [entry], "truncated": False}
    background = compress_evidence_for_prompt(evidence)
    assert background["entries"][0]["skeleton"]
    assert "crete" not in background["entries"][0]["content"]
    focused = compress_evidence_for_prompt(evidence, focus_paths={entry["path"]})
    assert focused["entries"] == [entry]
    assert "crete" in focused["entries"][0]["content"]
