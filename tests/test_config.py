from brindle.config import RepoConfig

def test_default_max_agents():
    cfg = RepoConfig()
    assert cfg.max_agents == 5
