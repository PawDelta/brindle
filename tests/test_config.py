"""Repo config: each setting from the person's own files over the repo's."""

from brindle import config


def test_auto_pr_comes_from_the_persons_config_and_their_repo_file(repo):
    config.set_user("auto_pr", True)
    assert config.load_repo_config(repo).auto_pr is True
    config.set_local(repo, "auto_pr", False)      # their own file for this repo wins
    assert config.load_repo_config(repo).auto_pr is False
