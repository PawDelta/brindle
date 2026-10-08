"""The GitLab host's ID tokens and what reaches agent panes (the host's own
API, merge request and init tests are in test_ci_gitlab.py)."""

from __future__ import annotations

import os

import pytest

from brindle import ci_client, ci_hosts, secrets

TOKENS = ("BRINDLE_ID_TOKEN", "BRINDLE_AWS_ID_TOKEN", "BRINDLE_GCP_ID_TOKEN", "BRINDLE_AZURE_ID_TOKEN", "CI_JOB_JWT_V2")


def test_every_id_token_of_the_template_is_scrubbed_from_the_job():
    for cloud in ci_hosts.CLOUD_ID_TOKENS:
        text = ci_hosts.render_template(cloud)
        declared = [t for t in TOKENS if f"    {t}:\n" in text]
        assert declared and all(secrets.is_job_secret(t) for t in declared)


def test_scrub_job_removes_all_id_tokens_from_env_and_process(monkeypatch):
    env = {t: "tok." + t + ".sig" for t in TOKENS}
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    ci_client.scrub_job(env, None, None)
    assert not [t for t in TOKENS if t in env or t in os.environ]


@pytest.mark.parametrize("name", TOKENS)
def test_no_pane_gets_an_id_token(name):
    for provider in ("claude", "codex", "antigravity"):
        assert name in secrets.pane_unset([name], keep=secrets.provider_credentials(provider, name))


def test_the_plain_template_still_declares_only_the_brindle_token():
    text = ci_hosts.GITLAB_TEMPLATE
    assert "BRINDLE_ID_TOKEN:" in text and "_ID_TOKEN:\n      aud: sts" not in text
    assert [t for t in TOKENS if f"    {t}:\n" in text] == ["BRINDLE_ID_TOKEN"]
