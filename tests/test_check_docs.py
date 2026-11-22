import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "check_docs", Path(__file__).resolve().parent.parent / "scripts/check_docs.py"
)
check_docs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check_docs)


def test_known_commands_and_keys_pass_and_unknowns_are_reported(tmp_path, capsys):
    clean_md = tmp_path / "clean.md"
    clean_md.write_text(
        "# Usage\n"
        "\n"
        "Run `brindle init`, `brindle -s codex start`, `brindle -- doctor`, `brindle agent kill`, or `brindle keys set/unset/list`.\n"
        "Run brindle doctor to check the environment.\n"
        "$ brindle doctor # brindle bogus-cmd is not supported\n"
        "Print a diagnostic with `echo \"brindle bogus-cmd\"`.\n"
        "\n"
        "`brindle doctor`\n"
        "\n"
        "And brindle handles the rest.\n"
        "Set `\"max_agents\": 4` in `.brindle/config.json`.\n"
        "\n"
        "In `package.json`:\n"
        "```json\n"
        '{"scripts": {"test": "pytest"}}\n'
        "```\n"
        "\n"
        "In `.brindle/config.json`:\n"
        "```json\n"
        '{"checks": ["pytest"], "routing": {"light": ["developer-light"]}}\n'
        "```\n"
        "\n"
        "## Repo config: `.brindle/config.json`\n"
        "\n"
        "| Key | Default |\n"
        "|---|---|\n"
        "| `stale_after` | `30` |\n",
        encoding="utf-8",
    )
    assert check_docs.main([str(clean_md)]) == 0
    assert capsys.readouterr().out == ""

    bad_md = tmp_path / "bad.md"
    bad_md.write_text(
        "# Broken docs\n"
        "\n"
        "Run `brindle doctor` and `brindle bogus-cmd`.\n"
        "Also `brindle agent nonexistent`, `brindle -s codex after-opt-nope`, and `brindle -- after-dd-nope`.\n"
        "The brindle unprefixed-nope command diagnoses problems.\n"
        "brindle agent bogusnested lists the agents.\n"
        "Run brindle doctor and brindle repeated-nope to diagnose problems.\n"
        "Run **brindle `emph-split-nope`**.\n"
        "Use `*.md` and run **brindle glob-bold-nope**.\n"
        "Set `\"fake_setting\": true` in `.brindle/config.json`.\n"
        "\n"
        "In `package.json`:\n"
        "```json\n"
        '{"scripts": {"test": "pytest"}}\n'
        "```\n"
        "\n"
        "In `.brindle/config.json`:\n"
        "```\n"
        "{\n"
        '  "checks": [],\n'
        '  "unknown_json_key": 1\n'
        "}\n"
        "```\n",
        encoding="utf-8",
    )

    bad_html = tmp_path / "bad.html"
    bad_html.write_text(
        "<p>Use <code>brindle ls</code> not <code>brindle html-nope</code>.</p>\n"
        "<p>Print a diagnostic with <code>echo \"brindle bogus-cmd\"</code>.</p>\n"
        "<p>The brindle html-unprefixed command and brindle <strong>inline-split-nope</strong> fail.</p>\n"
        "<p>brindle agent htmlnested lists agents; run brindle <code>code-split-nope</code> too.</p>\n"
        "<p>Set <code>\"html_bad_key\": 1</code> in <code>.brindle/config.json</code>.</p>\n"
        "<pre><code>brindle init\n"
        "brindle pre-nope\n"
        "</code></pre>\n",
        encoding="utf-8",
    )

    rc = check_docs.main([str(bad_md), str(bad_html)])
    assert rc == 1
    out = capsys.readouterr().out.splitlines()
    assert out == [
        f"{bad_md}:3: unknown command: brindle bogus-cmd",
        f"{bad_md}:4: unknown command: brindle after-dd-nope",
        f"{bad_md}:4: unknown command: brindle after-opt-nope",
        f"{bad_md}:4: unknown command: brindle agent nonexistent",
        f"{bad_md}:5: unknown command: brindle unprefixed-nope",
        f"{bad_md}:6: unknown command: brindle agent bogusnested",
        f"{bad_md}:7: unknown command: brindle repeated-nope",
        f"{bad_md}:8: unknown command: brindle emph-split-nope",
        f"{bad_md}:9: unknown command: brindle glob-bold-nope",
        f"{bad_md}:10: unknown config key: fake_setting",
        f"{bad_md}:21: unknown config key: unknown_json_key",
        f"{bad_html}:1: unknown command: brindle html-nope",
        f"{bad_html}:3: unknown command: brindle html-unprefixed",
        f"{bad_html}:3: unknown command: brindle inline-split-nope",
        f"{bad_html}:4: unknown command: brindle agent htmlnested",
        f"{bad_html}:4: unknown command: brindle code-split-nope",
        f"{bad_html}:5: unknown config key: html_bad_key",
        f"{bad_html}:7: unknown command: brindle pre-nope",
    ]
