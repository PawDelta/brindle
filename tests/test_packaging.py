import tomllib
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def test_project_urls_are_web_urls():
    """PyPI rejects an upload whose project URLs aren't http(s) (e.g. mailto:)."""
    urls = tomllib.loads(PYPROJECT.read_text())["project"]["urls"]
    assert urls and all(u.startswith("https://") for u in urls.values()), urls
