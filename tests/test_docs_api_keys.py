import re
from pathlib import Path


def test_docs_unattended_runs_on_api_keys():
    readme = Path("README.md").read_text()

    # 1. Structural heading and boundary validation
    heading = "#### Running unattended on API keys"
    assert heading in readme
    start = readme.find(heading)
    end = readme.find("### Cheap workers", start)
    assert end > start, "Expected 'Running unattended on API keys' section before '### Cheap workers'"
    section = readme[start:end]

    # Verify introductory sentence exists and introduces unattended autopilot on API keys
    intro_pattern = r"When running long,\s+unattended autopilot sessions with API keys:"
    assert re.search(intro_pattern, section, re.I), "Missing introductory context statement"

    # 2. Extract top-level bullet blocks (- **Title**: ...)
    raw_bullets = re.split(r"\n-\s+\*\*", section)
    bullet_dict = {}
    for block in raw_bullets[1:]:
        title, _, content = block.partition("**:")
        bullet_dict[title.strip()] = content.strip()

    # Verify all four required core topic areas are present as structural top-level bullets
    expected_topics = [
        "Quota, token and cost measurements per provider",
        "Runtime auth failure handling and provider differences",
        "Supervisor credential refresh and key rotation",
        "Antigravity permissions prerequisite",
    ]
    for topic in expected_topics:
        matching = [k for k in bullet_dict if topic.lower() in k.lower()]
        assert len(matching) == 1, f"Missing or duplicate top-level section: {topic}"

    # 3. Topic 1: Quota, token and cost measurements per provider
    topic1_body = [v for k, v in bullet_dict.items() if "quota, token and cost" in k.lower()][0]
    assert "usage_limit: 90" in topic1_body
    assert "ANTHROPIC_API_KEY" in topic1_body and "OPENAI_API_KEY" in topic1_body and "GEMINI_API_KEY" in topic1_body
    assert "report no subscription percentage" in topic1_body or "no subscription percentage" in topic1_body
    assert "no token/cost measurements" in topic1_body
    assert "antigravity" in topic1_body.lower() or "agy" in topic1_body.lower()
    assert "google ai studio" in topic1_body.lower() or "google cloud project" in topic1_body.lower()

    # 4. Topic 2: Runtime auth failure handling and provider differences
    topic2_body = [v for k, v in bullet_dict.items() if "runtime auth failure" in k.lower()][0]
    assert "claude and antigravity" in topic2_body.lower()
    assert "401/403" in topic2_body or "revoked" in topic2_body
    assert "credentials need attention" in topic2_body.lower()
    assert "codex" in topic2_body.lower()
    assert "unsupported runtime auth detection" in topic2_body.lower() or "agent-turn-complete" in topic2_body
    assert "does not classify auth errors at runtime" in topic2_body.lower()
    assert "brindle watch" in topic2_body or "tmux" in topic2_body

    # 5. Topic 3: Supervisor credential refresh and key rotation
    topic3_body = [v for k, v in bullet_dict.items() if "supervisor credential refresh" in k.lower()][0]
    assert "recovering failed/blocked workers" in topic3_body.lower()
    assert "brindle continue" in topic3_body
    assert "session credential store" in topic3_body.lower() or "dynamically refreshed" in topic3_body.lower()
    assert "proactive rotation in live sessions" in topic3_body.lower()
    assert "brindle stop" in topic3_body
    assert "precedence" in topic3_body.lower()
    assert "brindle keys set" in topic3_body
    assert "env.NAME" in topic3_body or "profile-level" in topic3_body.lower()

    # 6. Topic 4: Antigravity permissions prerequisite
    topic4_body = [v for k, v in bullet_dict.items() if "antigravity permissions" in k.lower()][0]
    assert "gemini_api_key" in topic4_body.lower()
    assert "settings.json" in topic4_body
    assert "agy_approvals" in topic4_body or "dangerously-skip-permissions" in topic4_body
