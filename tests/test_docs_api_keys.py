from pathlib import Path


def test_docs_unattended_runs_on_api_keys():
    readme = Path("README.md").read_text()
    assert "#### Running unattended on API keys" in readme
    start = readme.find("#### Running unattended on API keys")
    end = readme.find("### Cheap workers", start)
    section = readme[start:end]

    # Verify per-provider token and cost measurements
    assert "no token/cost measurements" in section.lower()
    assert "antigravity" in section.lower() or "agy" in section.lower()

    # Verify runtime auth failure handling differences (Claude/agy vs Codex)
    assert "codex" in section.lower()
    assert "turn completion" in section.lower() or "does not classify" in section.lower() or "unsupported" in section.lower()

    # Verify manual restart / key rotation procedure
    assert "brindle stop" in section
    assert "brindle continue" in section
