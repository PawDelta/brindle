import io
import json

import pytest

from brindle import agents, autopilot, cost_estimate, learned_rules, local_setup, quota
from brindle.config import RepoConfig
from brindle.db import Workspace
from brindle.profiles import Profile
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
        if ':8080/' in req.full_url and '/api/' in req.full_url:
            raise client.urllib.error.HTTPError(req.full_url, 404, 'not found', None, None)
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
    monkeypatch.setattr(runner, 'probe', lambda *a, **k: (True, ''))
    assert learned_rules.default_call(str(repo)).__name__ == ('native' if expected == 'developer-local' else 'claude')


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


def test_darwin_free_memory_does_not_count_speculative_pages_twice(monkeypatch, ollama):
    import ctypes
    import os
    import sys

    page = os.sysconf('SC_PAGE_SIZE')

    class Mach:
        restype = None

        def __call__(self):
            return 1

    class Libc:
        mach_host_self = Mach()

        @staticmethod
        def host_statistics64(host, flavor, stats, count):
            stats._obj[0] = 23 * 2**30 // page   # free_count, which already includes...
            stats._obj[23] = 3 * 2**30 // page   # ...speculative_count
            return 0

    monkeypatch.setattr(sys, 'platform', 'darwin')
    monkeypatch.setattr(ctypes, 'CDLL', lambda *a: Libc())
    assert local_setup.free_memory_gb() == pytest.approx(23, abs=0.01)
    assert local_setup.memory_problem(Profile('local', '', 'native', '', model='qwen3-coder:30b',
                                              base_url='http://localhost:11434/v1'))


def test_admitted_client_rechecks_memory_before_each_local_call(monkeypatch, ollama):
    free = iter([26, 1])
    monkeypatch.setattr(local_setup, 'free_memory_gb', lambda: next(free))
    local = client.Client(client.Endpoint('http://localhost:11434/v1', 'qwen3-coder:30b'))
    assert local.complete(None, [{'role': 'user', 'content': 'hello'}], []).text == 'done'
    chats = sum(url.endswith('/chat/completions') for url, _ in ollama)
    with pytest.raises(client.ClientError, match='insufficient free memory.*cloud profile'):
        local.complete(None, [{'role': 'user', 'content': 'again'}], [])
    assert sum(url.endswith('/chat/completions') for url, _ in ollama) == chats


def test_reviewer_resolver_falls_back_to_cloud_when_local_does_not_fit(db, tmp_path, monkeypatch, ollama, caplog):
    monkeypatch.setattr(local_setup, 'free_memory_gb', lambda: 1)
    monkeypatch.setattr(agents, 'unusable', lambda *a: None)
    monkeypatch.setattr(runner, 'served', lambda profile: True)
    ws = Workspace('w1', str(tmp_path), 'w1', 'worktree', 'b', 'main', str(tmp_path), None, 's', 0.0)
    cfg = RepoConfig(reviewer='reviewer-local', reviewer_fallbacks=['reviewer'])
    assert agents._review_profile(db, ws, None, cfg) == 'reviewer'
    assert 'insufficient free memory' in caplog.text
    assert not agents._local_reviewer_available()   # never offered to the learner


def test_non_ollama_loopback_server_is_not_gated(monkeypatch, ollama):
    monkeypatch.setattr(local_setup, 'free_memory_gb', lambda: 64)
    llama_cpp = Profile('local', '', 'native', '', model='qwen3-coder:30b', base_url='http://localhost:8080/v1')
    assert local_setup.memory_problem(llama_cpp) is None
