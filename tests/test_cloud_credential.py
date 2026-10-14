"""`brindle cloud-credential`: the hook the clouds run for a federated org."""

import base64
import io
import json
import os
import stat
import threading
import time
from types import SimpleNamespace

import pytest

from brindle import cloud_credential as cc
from brindle.pro import auth, credentials, team_policy
from brindle.pro.auth import TransportError
from brindle.pro.team_policy import AgentSetup, AwsFederation, AwsSetup, ClaudeSetup
from pro_fixtures import BASE, backend, claims, pro_env, sign, signing_key  # noqa: F401 - fixtures

ORG = "acme"
ROLE = "arn:aws:iam::123456789012:role/brindle-members-acme"
ASSUMED = "arn:aws:sts::123456789012:assumed-role/brindle-members-acme/brindle-me"
PATH = f"POST /orgs/{ORG}/federation/token"


def b64(obj):
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()


def id_token(now):
    return ".".join([b64({"alg": "RS256"}),
                     b64({"iat": int(now), "exp": int(now) + 600, "member": "sso:org:me", "fid": "fam-1"}),
                     "sig"])


class Fed:
    """PawDelta's mint route and the clouds' exchange, stubbed; counts both."""

    def __init__(self, backend):
        self.mints, self.sts_calls, self.headers = 0, [], []
        self.answer = None                      # (status, body) to send instead of a token
        self.at = time.time()                   # the clock the credentials are issued against
        self.entered, self.proceed = threading.Event(), threading.Event()
        self.proceed.set()                      # a test clears it to hold a mint open
        backend.routes[PATH] = self.mint

    def mint(self, form, headers):
        assert form == {"cloud": self.cloud}
        self.headers.append(headers)
        self.entered.set()
        assert self.proceed.wait(10)
        self.mints += 1
        if self.answer is not None:
            return self.answer
        return 200, {"id_token": id_token(self.at), "expires_in": 600}

    cloud = "aws"

    def sts(self, url, params):
        self.sts_calls.append((url, params))
        exp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.at + 900))
        return 200, (f"<AssumeRoleWithWebIdentityResponse xmlns='https://sts.amazonaws.com/doc/2011-06-15/'>"
                     f"<AssumeRoleWithWebIdentityResult><Credentials><AccessKeyId>ASIAEXAMPLE</AccessKeyId>"
                     f"<SecretAccessKey>s3cret</SecretAccessKey><SessionToken>tok</SessionToken>"
                     f"<Expiration>{exp}</Expiration></Credentials><AssumedRoleUser><Arn>{ASSUMED}</Arn>"
                     f"</AssumedRoleUser></AssumeRoleWithWebIdentityResult></AssumeRoleWithWebIdentityResponse>"
                     ).encode()


@pytest.fixture
def store(tmp_path):
    s = credentials.FileStore(tmp_path / "pro")
    s.save({"access_token": "at_0", "refresh_token": "cpr_0", "access_expires_at": time.time() + 900,
            "org_id": ORG, "base_url": BASE})
    return s


@pytest.fixture
def fed(backend, store, signing_key, monkeypatch):
    backend.access_tokens.add("at_0")
    backend.refresh_tokens["cpr_0"] = "active"
    # a 401 retry refreshes the entitlement of the stored org
    backend.routes[f"GET /entitlement?org_id={ORG}"] = lambda form, headers: (
        200, {"entitlement": sign(signing_key, claims(org_id=ORG)), "plan": "pro", "status": "active",
              "features": [], "seats": 5, "expires_at": int(time.time()) + 3600})
    claude = ClaudeSetup(route="bedrock", auth="federated", aws=AwsSetup(
        region="us-west-2", account_id="123456789012", role_name="brindle-members-acme",
        federation=AwsFederation(ROLE)))
    monkeypatch.setattr(team_policy, "load_cached",
                        lambda org: SimpleNamespace(agent_setup=AgentSetup(claude=claude)))
    return Fed(backend)


def call(fed, backend, store, cloud, org=ORG, now=None, **kw):
    fed.cloud = cloud
    out, err = io.StringIO(), io.StringIO()
    rc = cc.run(cloud, org, out=out, err=err, client=auth.Client(BASE, backend), store=store,
                sts=fed.sts, now=now, **kw)
    return rc, out.getvalue(), err.getvalue()


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


@pytest.mark.parametrize("cloud", ["aws", "gcp", "azure"])
def test_each_cloud_gets_what_its_library_reads_and_the_mint_sends_device_headers(cloud, fed, backend, store, caplog):
    import brindle

    rc, out, err = call(fed, backend, store, cloud, org=None)      # the stored org
    d = cc.org_dir(ORG)
    assert fed.headers[0]["X-Brindle-Version"] == brindle.__version__
    assert "X-Brindle-Device-Id" not in fed.headers[0]      # the server keys devices by token family
    if cloud == "aws":
        assert rc == 0 and err == ""
        assert json.loads(out) == {"Version": 1, "AccessKeyId": "ASIAEXAMPLE", "SecretAccessKey": "s3cret",
                                   "SessionToken": "tok", "Expiration": json.loads(out)["Expiration"]}
        url, params = fed.sts_calls[0]
        assert url == "https://sts.us-west-2.amazonaws.com/"
        assert params["RoleArn"] == ROLE and params["DurationSeconds"] == "900"
        assert params["RoleSessionName"].startswith("brindle-sso") and len(params["RoleSessionName"]) <= 64
        assert mode(d) == 0o700 and mode(d / cc.AWS_CACHE) == 0o600
        assert cc.aws_identity(ORG) == ("123456789012", "brindle-members-acme")
        assert call(fed, backend, store, "aws", now=fed.at + 300)[1] == out    # 600 of 900 s left: cached
        assert fed.mints == 1 and len(fed.sts_calls) == 1
        call(fed, backend, store, "aws", now=fed.at + 700)                     # 200 of 900 s left: mint again
        assert fed.mints == 2
        assert "s3cret" not in caplog.text and id_token(fed.at) not in caplog.text
        return
    if cloud == "gcp":
        got = json.loads(out)
        assert got == {"version": 1, "success": True, "token_type": cc.GCP_TOKEN_TYPE,
                       "id_token": id_token(fed.at), "expiration_time": int(fed.at) + 600}
        assert json.loads((d / cc.GCP_TOKEN).read_text()) == got        # the executable's output_file
        assert mode(d / cc.GCP_TOKEN) == 0o600
    else:
        assert out == "" and (d / cc.AZURE_TOKEN).read_text() == id_token(fed.at)
        assert mode(d / cc.AZURE_TOKEN) == 0o600
    assert rc == 0 and fed.sts_calls == []                              # the library, not brindle, exchanges


def test_a_personal_entitlement_mints_through_me_and_an_older_server_gets_a_readable_error(fed, backend, store, signing_key):
    backend.routes[PATH] = lambda form, headers: pytest.fail("a personal org must not use the org route")
    def mint(form, headers):
        status, body = fed.mint(form, headers)
        if status == 200:       # a personal token names the user, not an SSO member
            payload = json.loads(base64.urlsafe_b64decode(body["id_token"].split(".")[1] + "=="))
            payload.pop("member")
            body["id_token"] = f"{b64({'alg': 'RS256'})}.{b64({**payload, 'sub': 'user_1'})}.sig"
        return status, body

    backend.routes["POST /me/federation/token"] = mint
    cur = store.load()
    cur.pop("org_id")
    expired = claims(org_id=ORG, iat=int(fed.at) - 9000, exp=int(fed.at) - 4000)
    store.save({**cur, "entitlement": sign(signing_key, expired)})
    backend.routes["GET /entitlement"] = lambda form, headers: (
        200, {"entitlement": sign(signing_key, claims(org_id=ORG))})
    rc, out, err = call(fed, backend, store, "aws", org=None)     # the entitlement's own org
    assert rc == 0 and fed.mints == 1 and json.loads(out)["AccessKeyId"] == "ASIAEXAMPLE"
    assert fed.sts_calls[-1][1]["RoleSessionName"].startswith("brindle-user_1-")
    assert "GET /entitlement" in backend.paths()
    fed.answer = (404, {"error": "not_found"})
    rc, out, err = call(fed, backend, store, "aws", now=fed.at + 1000)
    assert rc == 1 and "doesn't offer federated credentials yet" in err


def test_apply_current_personal_federation(fed, backend, store, signing_key, monkeypatch):
    from brindle import company_login
    from brindle.pro.team_policy import CodexSetup, GcpFederation, GcpSetup, OrgPolicy

    cur = store.load()
    cur.pop("org_id")
    store.save({**cur, "entitlement": sign(signing_key, claims(org_id=ORG, policy_version=1))})
    setup = AgentSetup(claude=ClaudeSetup(route="vertex", auth="federated", gcp=GcpSetup(
        "acme-prod", "us-east5", GcpFederation("123456789", "pool", "pawdelta", "me@acme-prod.iam.gserviceaccount.com"))),
        codex=CodexSetup("chatgpt"), enforce=True)
    policy = OrgPolicy(ORG, 1, agent_setup=setup, cached_for=(None, None))
    monkeypatch.setattr(team_policy, "load_cached", lambda org: policy)
    monkeypatch.setattr(company_login, "run_cmd", lambda *a, **k: pytest.fail("personal Codex setup applied"))
    fed.cloud = "gcp"
    backend.routes["POST /me/federation/token"] = fed.mint
    assert company_login.apply_current(lambda line: None, store=store, client=auth.Client(BASE, backend))
    doc = json.loads(cc.gcp_config_path(ORG).read_text())
    assert doc["audience"].endswith("/workloadIdentityPools/pool/providers/pawdelta")
    env = company_login.config.user_settings()[company_login.ENV_KEY]["env"]
    assert env["GOOGLE_APPLICATION_CREDENTIALS"] == str(cc.gcp_config_path(ORG))
    assert json.loads((cc.org_dir(ORG) / cc.GCP_TOKEN).read_text())["id_token"] == id_token(fed.at)
    assert "POST /me/federation/token" in backend.paths() and fed.mints == 1


def test_azure_refresh_rewrites_the_file_only_below_30_percent(fed, backend, store, monkeypatch):
    fed.cloud = "azure"
    real = cc.credential
    monkeypatch.setattr(cc, "credential", lambda cloud, org, now=None: real(
        cloud, org, client=auth.Client(BASE, backend), store=store, now=now))
    path = cc.azure_token_path(ORG)
    assert cc.freshen_azure(path, now=fed.at) and fed.mints == 1            # no file: mint
    assert cc.freshen_azure(path, now=fed.at + 300) and fed.mints == 1      # 300 of 600 s left
    assert cc.freshen_azure(path, now=fed.at + 500) and fed.mints == 2      # 100 of 600 s left
    assert not cc.freshen_azure(path.parent / "elsewhere", now=fed.at)      # not a federation token file
    fed.answer = (503, {})
    assert not cc.freshen_azure(path, now=fed.at + 5000)                    # never raises

    # a tick can never skip past the threshold
    assert cc.AzureRefresher("x").interval < cc.REFRESH_BELOW * cc.AZURE_TOKEN_SECONDS

    # the session's refresher calls it on every tick until stopped
    seen, enough = [], threading.Event()

    def freshen(token_file):
        seen.append(token_file)
        if len(seen) == 3:
            enough.set()
        return True
    monkeypatch.setattr(cc, "freshen_azure", freshen)
    r = cc.AzureRefresher("/f/acme/azure-token", interval=0.01)
    r.start()
    assert enough.wait(10)
    r.stop()
    r._thread.join(10)
    assert not r._thread.is_alive() and set(seen) == {"/f/acme/azure-token"}


def test_reauth_required_deletes_the_store_and_prints_the_login_line(fed, backend, store):
    call(fed, backend, store, "aws")
    revoked = []
    backend.routes["POST /token/revoke"] = lambda form, headers: (
        revoked.append(bool(store.load())) or (200, {}))
    fed.answer = (401, {"error": "reauth_required"})
    rc, out, err = call(fed, backend, store, "aws", now=time.time() + 800)
    assert rc == 1 and out == "" and "brindle account login" in err
    assert not store.load() and revoked == [True]                      # signed out on the server while still stored
    assert cc.take_reauth() == ORG                                      # and the next login returns here
    assert not (cc.org_dir(ORG) / cc.AWS_CACHE).exists()                # no credential outlives the sign-in
    mints = fed.mints
    rc, out, err = call(fed, backend, store, "aws")                     # signed out: asks nobody
    assert (rc, out) == (1, "") and "brindle account login" in err and fed.mints == mints




def test_two_concurrent_invocations_make_one_mint(fed, backend, store):
    fed.proceed.clear()                                                 # the first mint stays open
    results = []
    first = threading.Thread(target=lambda: results.append(call(fed, backend, store, "aws")))
    second = threading.Thread(target=lambda: results.append(call(fed, backend, store, "aws")))
    first.start()
    assert fed.entered.wait(10)
    second.start()
    second.join(0.3)
    assert second.is_alive()                                            # waiting on the mint lock
    fed.proceed.set()
    first.join()
    second.join()
    assert fed.mints == 1 and len(fed.sts_calls) == 1
    assert [r[0] for r in results] == [0, 0] and results[0][1] == results[1][1]


def test_an_older_server_is_told_so_and_offline_serves_only_a_valid_cache(fed, backend, store):
    fed.answer = (404, {"error": "not_found"})
    rc, out, err = call(fed, backend, store, "aws")
    assert rc == 1 and out == "" and "doesn't offer federated credentials yet" in err
    assert store.load()                                                 # a 404 is no reason to sign out
    fed.answer = None
    first = call(fed, backend, store, "aws")[1]
    backend.routes[PATH] = [TransportError("down")]
    assert call(fed, backend, store, "aws", now=fed.at + 800)[1] == first   # stale but unexpired
    rc, out, err = call(fed, backend, store, "aws", now=fed.at + 1000)      # expired
    assert rc == 1 and out == "" and "can't reach" in err


# -- the hook command ----------------------------------------------------------------------------

def script(tmp_path, name="brindle", mode=0o755, directory="bin"):
    d = tmp_path / directory
    d.mkdir(exist_ok=True)
    os.chmod(d, 0o755)
    p = d / name
    p.write_text("#!/bin/sh\n")
    os.chmod(p, mode)
    return str(p)


def test_the_hook_command_names_the_running_script_or_the_interpreter(tmp_path):
    exe = script(tmp_path)
    assert cc.hook_command("aws", ORG, argv0=exe) == f'"{exe}" cloud-credential aws --org {ORG}'
    py = script(tmp_path, "python3")
    assert cc.hook_command("gcp", ORG, argv0="/x/pytest/__main__.py", python=py) == \
        f'"{py}" -m brindle cloud-credential gcp --org {ORG}'


@pytest.mark.parametrize("how", ["relative", "world_writable_file", "world_writable_dir", "quote", "missing", "not_executable"])
def test_a_hook_path_that_could_be_swapped_or_misread_is_refused(how, tmp_path):
    exe = script(tmp_path)
    if how == "relative":
        exe = "bin/brindle"
    elif how == "world_writable_file":
        os.chmod(exe, 0o777)
    elif how == "world_writable_dir":
        os.chmod(os.path.dirname(exe), 0o777)
    elif how == "quote":
        exe = script(tmp_path, directory='b"in')
    elif how == "missing":
        exe = str(tmp_path / "gone" / "brindle")
    else:
        os.chmod(exe, 0o644)
    with pytest.raises(cc.CredentialError):
        cc.hook_command("aws", ORG, argv0=exe)
