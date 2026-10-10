from pathlib import Path


def test_docs_unattended_runs_on_api_keys():
    readme = Path("README.md").read_text()
    assert "Running unattended on API keys" in readme
    assert "quota" in readme.lower()
    assert "usage pause" in readme.lower() or "usage limit" in readme.lower()
    assert "antigravity" in readme.lower() or "agy" in readme.lower()
