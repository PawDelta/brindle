import io
import json

import pytest

from brindle import agents, autopilot, cost_estimate, local_setup, quota
from brindle.config import RepoConfig
from brindle.native import client, runner, serve


@pytest.fixture(autouse=True)
def private_tmux_server():
    """These routing and HTTP tests do not use tmux."""


@pytest.fixture(autouse=True)
def test_tmux_server():
    """Override the shared per-test tmux setup as well."""


@pytest.fixture
def ollama(monkeypatch):
    requests = []

    def open_request(req, **kwargs):
        if isinstance(req, str):
            req = client.urllib.request.Request(req)
        requests.append((req.full_url, json.loads(req.data) if req.data else None))
        if req.full_url.endswith('/api/ps'):
            body = {'models': []}
        elif req.full_url.endswith('/api/tags'):
            body = {'models': [{'name': 'qwen3-coder:30b', 'size': 22 * 2**30}]}
        elif req.full_url.endswith('/chat/completions'):
            body = {'choices': [{'message': {'content': 'done'}}]}
        else:
            body = {}
        return io.BytesIO(json.dumps(body).encode())

    monkeypatch.setattr('urllib.request.urlopen', open_request)
    monkeypatch.setattr(client, '_opener', lambda: type('Opener', (), {'open': staticmethod(open_request)})())
    return requests


@pytest.mark.parametrize('free, expected', [(1, 'developer-light'),
                                            pytest.param(24, 'developer-light', marks=pytest.mark.variant('model fits but margin does not')),
                                            (26, 'developer-local')])
def test_routing_and_suggestions_respect_memory(db, tmp_path, monkeypatch, ollama, free, expected):
    repo = tmp_path
    monkeypatch.setattr(local_setup, 'free_memory_gb', lambda: free, raising=False)
    monkeypatch.setattr(agents, 'managed', lambda *a: None)
    monkeypatch.setattr(agents, '_local_reviewer_available', lambda: True)
    monkeypatch.setattr('brindle.providers.unusable', lambda *a: None)
    monkeypatch.setattr(quota, 'headroom', lambda *a: 100)
    cfg = RepoConfig(routing={'light': ['developer-local', 'developer-light']},
                     routing_locked={'light': True})
    monkeypatch.setattr(autopilot, 'load_repo_config', lambda *a: cfg)
    picked, _ = autopilot.choose_profile(db, '', str(repo), weight='light')
    assert picked == expected
    assert autopilot.choose_profile(db, '', str(repo), requested='developer-local', weight='light')[0] == expected
    candidates = cost_estimate.available_reviewers(str(repo))
    assert ('reviewer-local' in candidates) == (expected == 'developer-local')
    alternative = cost_estimate.cheaper([cost_estimate.TaskSpec('developer-light', 'light')], 'reviewer', ['reviewer-local'],
                                       dists={}, price=lambda *a: 1, repo_root=str(repo))
    assert (alternative is not None) == (expected == 'developer-local')
    server = serve.Server('http://localhost:11434', ['qwen3-coder:30b'])
    assert serve.warm(server, 'qwen3-coder:30b') == (expected == 'developer-local')
    monkeypatch.setattr(runner, 'served', lambda profile: True)
    fallback = runner.refusal_fallback(client.Endpoint('https://api.example.com/v1', 'cloud'), str(repo))
    assert (fallback is not None) == (expected == 'developer-local')


def test_local_calls_unload_after_completion(monkeypatch, ollama):
    monkeypatch.setattr(local_setup, 'free_memory_gb', lambda: 26)
    reply = client.Client(client.Endpoint('http://localhost:11434/v1', 'qwen3-coder:30b')).complete(
        None, [{'role': 'user', 'content': 'hello'}], [])
    assert reply.text == 'done'
    assert all(body.get('keep_alive') == '0' for _, body in ollama if body)
    # The compatibility endpoint may ignore keep_alive: the native unload must follow it.
    assert ollama[-1][0].endswith('/api/generate')
    server = serve.Server('http://localhost:11434', ['qwen3-coder:30b'], ['developer-local'])
    assert serve.warm(server, 'qwen3-coder:30b')
    assert ollama[-1][1]['keep_alive'] == '0'
