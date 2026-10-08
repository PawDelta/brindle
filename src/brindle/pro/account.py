"""The ``pro`` plugin in brindle's ``brindle.account`` entry-point group:
``brindle account [features]|login|logout|status|upgrade|portal|org|license``."""

from __future__ import annotations

import sys
import time
from datetime import datetime, timezone

from brindle.pro import auth, credentials, license, loopback

PRICING_URL = "https://pawdelta.com/brindle#pricing"
ENTERPRISE_URL = "https://pawdelta.com/brindle/enterprise"

# The paid features: (entitlement feature, cheapest plan with it, what it is, how to use it).
FEATURES = (
    ("learning", "pro", "hosted learning: picks the best profile per task",
     "on by itself; see `brindle learning`"),
    ("services", "pro", "per-worktree Docker services (db, cache)",
     '"services" in .brindle/config.json'),
    ("multi_repo", "pro", "work across several repos in one session", "`brindle repo add <path>`"),
    ("settings_sync", "pro", "your settings follow you across machines", "`brindle account sync`"),
    ("ci_fix", "pro", "Brindle-CI build fixing (the fix workflow)", "`brindle ci init`"),
    ("learned_rules", "pro", "rules suggested from repeated review findings", "`brindle rules suggest`"),
    ("cost", "pro", "cost estimates, budgets, cost and savings reports", "`brindle cost report`"),
    ("guardrails", "pro", "write scope and env allowlist per profile",
     "write_scope, read_scope, env_allow in a profile"),
    ("team", "team", "org policies + team audit feed", "`brindle account org policy`"),
    ("ci", "team", "Brindle-CI: fixes builds, issues into verified PRs", "`brindle ci init`"),
    ("org_profiles", "team", "org profile and rule-pack library, pinned",
     "`brindle account org profiles`"),
    ("org_budgets", "team", "org budgets and protected paths", "`budget`, `protected_paths` in your org policy (`brindle account org policy`)"),
    ("audit", "enterprise", "tamper-evident local audit log", "`brindle audit verify`"),
    ("airgap", "enterprise", "air-gapped mode, local models only",
     '"airgap": true in .brindle/config.json'),
    ("managed_models", "enterprise", "org-managed provider config (Bedrock, Vertex...)", "`provider_config`, `deny_personal_keys` in your org policy (`brindle account org policy`); `brindle doctor` shows the provider"),
    ("audit_export", "enterprise", "audit export to webhook, Splunk, Datadog, S3", "`brindle audit ship`"),
    ("ci_enterprise", "enterprise", "wider CI: no cap, your runners, beyond GitHub", "`brindle ci init --host gitlab`"),
    ("cost_centers", "enterprise", "spend by cost center, admin-approved overruns",
     "`brindle cost request`"),
    ("managed_rollout", "enterprise", "min version, required profiles, kill switch",
     "`min_version`, `required_profiles`, `required_rule_packs`, `kill_switch` in your org policy (`brindle account org policy`)"),
)

TRIAL_MAX_SEATS = 10   # an Enterprise trial is capped here (the backend enforces it too)

USAGE = """usage: brindle account [<command>] [--base-url URL]

  (none)    what brindle Pro/Team add, which you have, and how to get the rest
  features  the same
  login     log in to brindle Pro: opens your browser and waits for it to come back
  login --device
            show a code to enter in a browser elsewhere instead (SSH, no browser here)
  logout    revoke this device's session and forget its credentials
  status    show your account, plan, features and when the entitlement expires
  savings   what hosted learning's picks gained in this repo, this month and last
            (estimates, from this machine's records only)
  upgrade   open the checkout for brindle Pro (your personal org); prints the URL too
  upgrade --team --seats N [--org ORG]
            print the checkout URL for brindle Team on a team org you administer
            (default: the current org)
  upgrade --enterprise [--trial] --seats N [--org ORG]
            the same for brindle Enterprise; --trial starts the free trial
            (at most 10 seats)
  portal [--org ORG]  open the billing portal (invoices, payment method, cancellation)
  seats N [--org ORG]  set a Team or Enterprise org's seat count (billing admin); Stripe
            prorates the change on your next invoice
  sync      sync your user-wide settings (~/.brindle/config.json) with your account now
  org list          list the orgs you belong to
  org create <name> create a team org you own (then `upgrade --team`)
  org invite <email> [--admin]  invite someone to the current org; prints the code
  org join <code>   accept an invite code and join that org
  org use <org_id>  work as a member of <org_id> (`personal` for your own)
  org policy        show the current org's policy, its per-role overrides and the
                    policy that applies to you (and refresh the cached copy)
  org profiles [list] [--org ORG]
            list the org's shared profiles and rule packs (and refresh the cached copy)
  org profiles push <file> [--pack] [--pinned] [--name NAME] [--org ORG]
            publish a profile (or, with --pack, a rule pack) to the org (admin+);
            --pinned: a repo or user file can't replace it
  org profiles rm <name> [--pack] [--org ORG]
            take a profile (or rule pack) out of the org's library (admin+)
  org member policy-role <member> <role|none> [--org ORG]
            give a member (their account id) a policy role, or none (admin+)
  org company [--org ORG]  show the company this org is linked into
  org company link <org_id> [--org ORG]
            link this org and <org_id> into one company (you must own both)
  org company unlink [--org ORG]  take this org out of its company
  org learning-share [on|off] [--org ORG]
            show, or turn on/off, pooling this org's coarse learning records with
            the other orgs in its company, never with other companies
            (off by default; owner/admin to change)
  org ci-token create <name> [--org ORG]
                    create a CI token for brindle CI (admin+); shown once
  org ci-token list [--org ORG]           list the org's CI tokens
  org ci-token revoke <token_id> [--org ORG]  revoke a CI token
  license install <file>  install an offline (brindle Enterprise) license; verified with the
                    pinned keys, no network; used in air-gap mode and when not logged in
  license status    show the installed offline license and the air-gap status
  license remove    remove the installed offline license"""


def _take(args: list[str], flag: str, value: bool = True):
    """Remove ``flag`` (and its value) from ``args``; return the value, True
    for a bare flag, or None if absent. Raises ValueError if the value is missing."""
    if flag not in args:
        return None
    i = args.index(flag)
    if not value:
        del args[i]
        return True
    if i + 1 >= len(args) or args[i + 1].startswith("--"):
        raise ValueError(flag)
    v = args[i + 1]
    del args[i:i + 2]
    return v


def _when(ts: int | None) -> str:
    if not ts:
        return "-"
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


class _OrgCommands:
    def cmd_org_list(self, base: str | None) -> int:
        client = self._client(base)
        status, body = auth.authed(client, self.store, "GET", "/orgs")
        if status != 200:
            raise auth._error(status, body)
        current = self._current_org()
        orgs = body.get("orgs") if isinstance(body.get("orgs"), list) else []
        if not orgs:
            self._say("You don't belong to any orgs.")
            return 0
        for o in orgs:
            if not isinstance(o, dict):
                continue
            org_id = auth._sanitize(o.get("org_id", ""), 64)
            mark = "*" if org_id == current else " "
            kind = "personal" if o.get("personal") else "team"
            self._say(f"{mark} {org_id:<28} {auth._sanitize(o.get('name', ''), 40):<32} "
                      f"{kind:<8} {auth._sanitize(o.get('role', ''), 8):<7} "
                      f"{auth._sanitize(o.get('plan', ''), 12)}")
        return 0

    def _current_org(self) -> str | None:
        try:
            creds = self.store.load() or {}
        except credentials.CredentialError:
            return None
        if creds.get("org_id"):
            return creds["org_id"]
        try:
            return license.verify(creds.get("entitlement") or "", issuer=auth.base_url(
                creds.get("base_url")), grace=license.MAX_GRACE).org_id
        except (license.LicenseError, auth.AuthError):
            return None

    def _team_org(self, org: str | None) -> str:
        """The team org a command acts on: ``org`` or the current one."""
        org = org or (self.store.load() or {}).get("org_id")
        if not org:
            raise auth.AuthError("no team org selected: pass --org ORG or run "
                                 "`brindle account org use <org_id>` (see `brindle account org list`)",
                                 code="no_team_org")
        if not auth.ORG_ID_RE.match(org):
            raise auth.AuthError("invalid org id", code="bad_request")
        return org

    def cmd_org_create(self, base: str | None, *name_words: str) -> int:
        name = " ".join(name_words).strip()
        if not name or len(name) > 100:
            raise auth.AuthError("org name must be 1-100 characters", code="bad_request")
        org = auth.create_org(self._client(base), self.store, name)
        self._say(f"Created team org {org['org_id']} ({org['name']}); you are its owner.")
        self._say(f"Next: brindle account upgrade --team --seats N --org {org['org_id']}")
        return 0

    def cmd_org_invite(self, base: str | None, email: str, org: str | None = None,
                       admin: bool = False) -> int:
        org_id = self._team_org(org)
        inv = auth.create_invite(self._client(base), self.store, org_id, email,
                                 "admin" if admin else "member")
        self._say(f"Invited {inv['email']} to {inv['org_id']} as {inv['role']}.")
        self._say(f"Send them this command (the code is shown once): "
                  f"brindle account org join {inv['invite_code']}")
        return 0

    def cmd_org_join(self, base: str | None, code: str) -> int:
        got = auth.accept_invite(self._client(base), self.store, code.strip())
        self._say(f"Joined {got['org_id']} as {got['role']}. "
                  f"Run `brindle account org use {got['org_id']}` to work as a member.")
        return 0

    def cmd_org_ci_token(self, base: str | None, action: str, *rest: str, org: str | None = None) -> int:
        org_id = self._team_org(org)
        client = self._client(base)
        if action == "create":
            name = " ".join(rest).strip()
            if not name or len(name) > 100:
                raise auth.AuthError("CI token name must be 1-100 characters", code="bad_request")
            got = auth.create_ci_token(client, self.store, org_id, name)
            self._say(f"Created CI token {got['token_id']} ({got['name']}) for {got['org_id']}.")
            self._say("Store it as the BRINDLE_PRO_TOKEN repository secret (it is shown once):")
            self._say(f"  {got['token']}")
            self._say("  e.g. gh secret set BRINDLE_PRO_TOKEN   (then paste it)")
            return 0
        if action == "list":
            tokens = auth.list_ci_tokens(client, self.store, org_id)
            if not tokens:
                self._say(f"Org {org_id} has no CI tokens.")
                return 0
            for t in tokens:
                self._say(f"{t['token_id']:<36} {t['name'][:32]:<32} {t['status']:<8} "
                          f"created {_when(t['created_at'])} by {t['created_by']}, "
                          f"last used {_when(t['last_used_at'])}")
            return 0
        auth.revoke_ci_token(client, self.store, org_id, rest[0])
        self._say(f"Revoked CI token {rest[0]}; runs using it stop at their next start.")
        return 0

    def cmd_org_profiles(self, base: str | None, action: str = "list", *rest: str,
                         org: str | None = None, pack: bool = False, pinned: bool = False,
                         name: str | None = None) -> int:
        from brindle.pro import org_profiles

        kind = "pack" if pack else "profile"
        client = org_profiles.client_for(self._client(base).base, self.transport)
        if action == "list":
            ent = license.current(store=self.store, client=client)
            org_id = self._team_org(org) if org else ent.org_id
            if org_profiles.FEATURE not in ent.features and org_id == ent.org_id:
                self._say(f"Org {org_id} has no profile library (plan {ent.plan}).")
                return 0
            try:
                lib = org_profiles.fetch_library(org_id, client, self.store)
                note = ""
            except org_profiles.LibraryUnavailable as e:
                lib = org_profiles.load_cached(org_id)
                if lib is None:
                    self._say(f"brindle account: couldn't fetch the library for {org_id} ({e}), "
                              "and none is cached.")
                    return 1
                note = f" (cached; couldn't refresh: {e})"
            self._say(f"Library for org {lib.org_id}, version {lib.version}{note}")
            for label, group in (("Profiles", lib.profiles), ("Rule packs", lib.packs)):
                self._say(f"{label}:" + ("" if group else " none"))
                for item in group.values():
                    self._say(f"  {auth._sanitize(item.name, 80)}" + ("  (pinned)" if item.pinned else ""))
            return 0
        org_id = self._team_org(org)
        if action == "push":
            item = org_profiles.read_item(rest[0], kind, name=name, pinned=pinned)
            got = org_profiles.push(client, self.store, org_id, publish=[item])
            self._say(f"Published {kind} {item['name']}" + (" (pinned)" if pinned else "")
                      + f" to org {org_id}; library version {got.get('version')}.")
        else:
            if not org_profiles.NAME_RE.match(rest[0]):
                raise auth.AuthError(f"{rest[0]!r} is not a valid org {kind} name", code="bad_request")
            got = org_profiles.push(client, self.store, org_id,
                                    delete=[{"kind": kind, "name": rest[0]}])
            self._say(f"Removed {kind} {rest[0]} from org {org_id} (if it was there); "
                      f"library version {got.get('version')}.")
        try:   # bring this machine's copy up to date
            org_profiles.fetch_library(org_id, client, self.store)
        except org_profiles.LibraryUnavailable:
            pass
        return 0

    def cmd_org_learning_share(self, base: str | None, *state: str, org: str | None = None) -> int:
        from brindle import airgap

        if airgap.enabled():
            raise auth.AuthError(f"air-gap mode is on via {airgap.source()}: nothing is shared and "
                                 "learning sharing is unavailable", code="airgap")
        org_id = self._team_org(org) if org else self._current_org_required()
        client = self._client(base)
        if state:
            got = auth.set_learning_sharing(client, self.store, org_id, state[0] == "on")
        else:
            got = auth.get_learning_sharing(client, self.store, org_id)
        self._say(f"Learning sharing for {org_id}: {'ON' if got['enabled'] else 'off'}"
                  + (f" (changed {_when(got['updated_at'])})" if got["updated_at"] else "")
                  + ".")
        self._say("When on, this org's coarse learning records (task kind, size, weight, profile, "
                  "cost, outcome),")
        self._say("the same ones already sent for hosted learning, under this org's own hashes, "
                  "are pooled")
        self._say("only with the other orgs in the same company, never with other companies. "
                  "An org that")
        self._say("is in no company pools with no one (`brindle account org company`).")
        self._say("Never task text, paths or names; only brindle's built-in profile names are "
                  "pooled, custom")
        self._say("ones never leave the org. The pool only gives a starting point: your own "
                  "data still wins")
        self._say("once you have a little of it. Turning it off stops new contributions, and "
                  "what was")
        self._say("shared fades out over time.")
        self._say("Off by default.")
        if not state:
            self._say("Change it (owner/admin): `brindle account org learning-share on|off`.")
        return 0

    def cmd_org_company(self, base: str | None, *action: str, org: str | None = None) -> int:
        org_id = self._team_org(org) if org else self._current_org_required()
        client = self._client(base)
        try:
            if action[:1] == ("link",):
                got = auth.link_company(client, self.store, org_id, action[1])
            elif action:
                got = auth.unlink_company(client, self.store, org_id)
            else:
                got = auth.get_company(client, self.store, org_id)
        except auth.AuthError as e:
            if action and e.code == "forbidden":
                raise auth.AuthError(f"{e} (only someone who owns both orgs can change "
                                     "their company link)", code=e.code) from e
            raise
        if got["company_id"]:
            self._say(f"Org {org_id} is in company {got['company_id']}.")
        else:
            self._say(f"Org {org_id} is not linked into a company.")
        self._say("Learning is pooled only between the orgs of one company, never across "
                  "companies, and only")
        self._say("for orgs that turned sharing on (`brindle account org learning-share`).")
        if not action:
            self._say("Link two orgs you own: `brindle account org company link <org_id>`; "
                      "undo it with `unlink`.")
        return 0

    def cmd_org_member(self, base: str | None, action: str, member: str, role: str,
                       org: str | None = None) -> int:
        org_id = self._team_org(org) if org else self._current_org_required()
        try:
            got = auth.set_policy_role(self._client(base), self.store, org_id, member,
                                       None if role == "none" else role)
        except auth.AuthError as e:
            if e.code == "enterprise_required":
                raise auth.AuthError(f"{e} (policy roles need brindle Enterprise: {ENTERPRISE_URL})",
                                     code=e.code) from e
            raise
        if got["policy_role"]:
            self._say(f"Member {got['sub']} of {org_id} now has the policy role "
                      f"{got['policy_role']}.")
        else:
            self._say(f"Member {got['sub']} of {org_id} has no policy role now; "
                      "their org role's policy applies.")
        self._say("See what each role gets with `brindle account org policy`.")
        return 0

    def _current_org_required(self) -> str:
        org = self._current_org()
        if not org:
            raise auth.AuthError("no org selected: pass --org ORG or run `brindle account login`",
                                 code="no_team_org")
        return org

    def cmd_org_use(self, base: str | None, org_id: str) -> int:
        target = None if org_id == "personal" else org_id
        ent = auth.switch_org(self._client(base), self.store, target)
        self._say(f"Now using org {ent.org_id} as {ent.role or 'member'} (plan {ent.plan}).")
        return 0

    def cmd_org_policy(self, base: str | None) -> int:
        from brindle.pro import team_policy

        client = self._client(base)
        ent = license.current(store=self.store, client=client)
        if "team" not in ent.features:
            self._say(f"Org {ent.org_id} has no team policy (plan {ent.plan}); nothing is enforced.")
            return 0
        from brindle import airgap

        try:
            if airgap.enabled():     # nothing is fetched: the policy is the offline file's
                p = team_policy.with_role_overrides(
                    team_policy.load_offline(self.repo_root, ent.org_id), ent.role, ent.policy_role)
                note = f" (offline: {airgap.policy_path(self.repo_root)})"
            else:
                p = team_policy.fetch_policy(ent.org_id, client, self.store,
                                             cached_for=(ent.role, ent.policy_role))
                note = ""
        except team_policy.PolicyUnavailable as e:
            if airgap.enabled():
                self._say(f"brindle account: no usable offline policy for {ent.org_id} ({e}); "
                          f"delegations and merges are refused until it is at .brindle/{airgap.POLICY_FILE}.")
                return 1
            p = team_policy.load_cached(ent.org_id)
            if p is None:
                self._say(f"brindle account: couldn't fetch the policy for {ent.org_id} ({e}), "
                          "and none is cached; delegations and merges are refused until it is.")
                return 1
            note = f" (cached; couldn't refresh: {e})"
        fmt = lambda v: "any" if v is None else (", ".join(v) or "none")  # noqa: E731
        show = {"allowed_providers": ("providers", fmt), "allowed_models": ("models", fmt),
                "allowed_profiles": ("profiles", fmt),
                "require_human_review": ("require human review", lambda v: "yes" if v else "no"),
                "max_parallel_workers": ("max parallel workers", lambda v: v or "no limit")}

        def rules(q) -> None:
            for key, (label, f) in show.items():
                self._say(f"  {label:<22} {f(getattr(q, key))}")

        self._say(f"Policy for org {p.org_id}, version {p.version}{note}")
        rules(p)
        if p.roles:
            self._say("Role overrides (what a role's members get instead):")
            for role, over in sorted(p.roles.items()):
                self._say(f"  {role}")
                for key, v in over.items():
                    self._say(f"    {show[key][0]:<22} {show[key][1](v)}")
        else:
            self._say("Role overrides: none")
        role, policy_role = p.role or ent.role, p.policy_role or ent.policy_role
        who = f"role {role or 'member'}" + (f", policy role {policy_role}" if policy_role else "")
        if p.effective is None:
            self._say(f"Your policy ({who}): the org policy above.")
        else:
            self._say(f"Your effective policy ({who}):")
            rules(p.effective)
        return 0


class ProAccount(_OrgCommands):
    def __init__(self, repo_root: str | None = None, store=None, transport=None,
                 out=None, err=None) -> None:
        self.repo_root = repo_root
        self._store, self.transport = store, transport
        self.out, self.err = out or sys.stdout, err or sys.stderr

    @property
    def store(self):
        if self._store is None:
            self._store = credentials.default_store()
        return self._store

    def _say(self, msg: str) -> None:
        print(msg, file=self.out)

    def _client(self, base: str | None) -> auth.Client:
        if base is None:
            try:
                base = (self.store.load() or {}).get("base_url")
            except credentials.CredentialError:
                base = None
        return auth.Client(base, transport=self.transport)

    def run(self, args: list[str]) -> int:
        args = list(args)
        base = None
        if "--base-url" in args:
            i = args.index("--base-url")
            if i + 1 >= len(args):
                print(USAGE, file=self.err)
                return 2
            base = args[i + 1]
            del args[i:i + 2]
        if args and args[0] in ("-h", "--help"):
            print(USAGE, file=self.out)
            return 0
        try:
            opts = {"team": _take(args, "--team", value=False),
                    "enterprise": _take(args, "--enterprise", value=False),
                    "trial": _take(args, "--trial", value=False), "seats": _take(args, "--seats"),
                    "org": _take(args, "--org"), "admin": _take(args, "--admin", value=False),
                    "device": _take(args, "--device", value=False),
                    "pack": _take(args, "--pack", value=False),
                    "pinned": _take(args, "--pinned", value=False), "name": _take(args, "--name")}
            seats = int(opts["seats"]) if opts["seats"] is not None else None
        except ValueError:
            print(USAGE, file=self.err)
            return 2
        cmd, rest = (args[0] if args else "features"), args[1:]
        sub = None
        if cmd == "org":
            sub = rest[0] if rest else "list"
        elif cmd == "license":
            sub = rest[0] if rest else "status"
        ok = {
            "features": not rest, "login": not rest, "logout": not rest, "status": not rest,
            "upgrade": not rest and not (opts["team"] and opts["enterprise"])
            and (bool(opts["team"] or opts["enterprise"]) == (seats is not None))
            and (seats is None or seats >= 1)
            and (opts["org"] is None or bool(opts["team"] or opts["enterprise"]))
            and (not opts["trial"] or bool(opts["enterprise"])),
            "portal": not rest, "sync": not rest, "savings": not rest, "seats": len(rest) == 1,
            "org": (sub in ("list", "policy") and len(rest) <= 1)
            or (sub in ("use", "invite", "join") and len(rest) == 2)
            or (sub == "learning-share" and len(rest) <= 2 and rest[1:] in ([], ["on"], ["off"]))
            or (sub == "company" and (rest[1:] in ([], ["unlink"])
                                      or (len(rest) == 3 and rest[1] == "link")))
            or (sub == "member" and len(rest) == 4 and rest[1] == "policy-role")
            or (sub == "profiles" and (len(rest) == 1 or (rest[1] == "list" and len(rest) == 2)
                                       or (rest[1] in ("push", "rm") and len(rest) == 3)))
            or (sub == "create" and len(rest) >= 2)
            or (sub == "ci-token" and len(rest) >= 2 and (
                (rest[1] == "create" and len(rest) >= 3) or (rest[1] == "list" and len(rest) == 2)
                or (rest[1] == "revoke" and len(rest) == 3))),
            "license": (sub in ("status", "remove") and len(rest) <= 1)
            or (sub == "install" and len(rest) == 2),
        }.get(cmd, False)
        flags_ok = {"upgrade": ("team", "enterprise", "trial", "seats", "org"), "portal": ("org",), "seats": ("org",), "login": ("device",),
                    "org": ("org", "admin") if sub == "invite"
                    else ("org",) if sub in ("ci-token", "learning-share", "company", "member")
                    else ("org", "pack", "pinned", "name") if sub == "profiles" and rest[1:2] == ["push"]
                    else ("org", "pack") if sub == "profiles" and rest[1:2] == ["rm"]
                    else ("org",) if sub == "profiles"
                    else ()}.get(cmd, ())
        if not ok or any(v is not None and k not in flags_ok for k, v in opts.items()):
            print(USAGE, file=self.err)
            return 2
        try:
            if cmd == "license":
                return getattr(self, "cmd_license_" + sub)(base, *rest[1:])
            if cmd == "org":
                if sub == "invite":
                    return self.cmd_org_invite(base, rest[1], opts["org"], bool(opts["admin"]))
                if sub == "ci-token":
                    return self.cmd_org_ci_token(base, *rest[1:], org=opts["org"])
                if sub == "learning-share":
                    return self.cmd_org_learning_share(base, *rest[1:], org=opts["org"])
                if sub == "profiles":
                    return self.cmd_org_profiles(base, *rest[1:], org=opts["org"],
                                                 pack=bool(opts["pack"]), pinned=bool(opts["pinned"]),
                                                 name=opts["name"])
                if sub in ("company", "member"):
                    return getattr(self, "cmd_org_" + sub)(base, *rest[1:], org=opts["org"])
                return getattr(self, "cmd_org_" + sub)(base, *rest[1:])
            if cmd == "upgrade":
                return self.cmd_upgrade(base, team=bool(opts["team"]), seats=seats, org=opts["org"],
                                        enterprise=bool(opts["enterprise"]), trial=bool(opts["trial"]))
            if cmd == "portal":
                return self.cmd_portal(base, org=opts["org"])
            if cmd == "seats":
                return self.cmd_seats(base, rest[0], org=opts["org"])
            if cmd == "login":
                return self.cmd_login(base, device=bool(opts["device"]))
            return getattr(self, "cmd_" + cmd)(base)
        except (auth.AuthError, license.LicenseError, credentials.CredentialError) as e:
            print(f"brindle account: {e}", file=self.err)
            return 1

    def _open(self, url: str) -> None:
        """Print ``url``; also open it in the browser when talking to a terminal."""
        self._say(url)
        if getattr(self.out, "isatty", lambda: False)():
            import webbrowser

            try:
                webbrowser.open(url)
            except Exception:  # noqa: BLE001 - the printed URL is enough
                pass

    def cmd_sync(self, base: str | None) -> int:
        from brindle.pro import settings_sync

        client = self._client(base)
        if not settings_sync.entitled(store=self.store):
            # A license signed before the plan gained settings sync: fetch a
            # fresh one once, rather than wait for its scheduled renewal.
            try:
                auth.refresh(client, self.store)
                license.clear_cache()
            except Exception:  # noqa: BLE001 - offline or logged out: sync says why
                pass
        r = settings_sync.sync(client=client, store=self.store)
        if r.action == "skipped":
            self._say(f"Settings not synced: {r.reason}")
            return 0
        verb = {"pulled": "Updated from your account", "pushed": "Sent to your account",
                "unchanged": "Settings already in sync"}[r.action]
        self._say(verb + (":" if r.changes else "."))
        for key, (old, new) in sorted(r.changes.items()):
            self._say(f"  {key}: {'-' if old is None else old} -> {'-' if new is None else new}")
        return 0

    def cmd_features(self, base: str | None) -> int:
        try:
            ent = license.current(store=self.store, client=self._client(base))
            err = None
        except (license.LicenseError, auth.AuthError, credentials.CredentialError) as e:
            ent, err = None, e
        have = ent.features if ent else frozenset()
        if ent:
            self._say(f"brindle {ent.plan.capitalize()}: org {ent.org_id}"
                      + (" (offline grace)" if ent.in_grace else ""))
        elif err and "not logged in" not in str(err):
            self._say(f"brindle Pro: {err}")
        else:
            self._say("brindle Pro: not logged in. brindle is complete without it; paid plans add:")
        self._say("")
        for feature, plan, what, how in FEATURES:
            if feature in have:
                self._say(f"  ✓ {feature:<15} {what:<50} {how}")
            else:
                self._say(f"    {feature:<15} {what:<50} needs {plan.capitalize()}")
        self._say("")
        if ent and "learning" in have and not ent.in_grace:
            self._learning_share_line(base, ent.org_id)
            line = self._savings_line()
            if line:
                self._say(line)
        self._say("")
        if ent is None:
            self._say("Next: `brindle account login`, then `brindle account upgrade`. "
                      f"Plans: {PRICING_URL}")
        elif not have & {"learning", "services"}:
            self._say(f"Next: `brindle account upgrade` opens the checkout. Plans: {PRICING_URL}")
        elif "team" not in have:
            self._say("Next, for a team: `brindle account org create NAME`, then "
                      "`brindle account upgrade --team --seats N --org ORG`. "
                      f"Plans: {PRICING_URL}")
        elif "audit" not in have:
            self._say(f"Enterprise (managed models, audit export, SCIM, air-gap) is sales-led: {ENTERPRISE_URL}")
        self._say("More: `brindle account status` (your plan), `brindle account --help` (all commands).")
        return 0

    def _savings_line(self) -> str | None:
        """The one-line local savings summary, or None if it can't be read."""
        if not self.repo_root:
            return None
        try:
            from brindle import savings
            from brindle.db import DB

            return savings.summary_line(savings.report(DB(), self.repo_root))
        except Exception:  # noqa: BLE001 - a side note; never fail `brindle account` over it
            return None

    def cmd_savings(self, base: str | None) -> int:
        """Local records only: no login, no network."""
        from brindle import savings
        from brindle.db import DB

        if not self.repo_root:
            self._say("brindle account savings: run it inside a repo.")
            return 1
        self._say(savings.describe(savings.report(DB(), self.repo_root), self.repo_root))
        self._say("Org admins can see org-wide totals on the brindle account page "
                  "(pawdelta.com/brindle/account).")
        return 0

    def _learning_share_line(self, base: str | None, org_id: str) -> None:
        """One best-effort line for bare ``brindle account``; silent when it can't be read."""
        from brindle import airgap

        if airgap.enabled():
            return
        try:
            got = auth.get_learning_sharing(self._client(base), self.store, org_id)
        except Exception:  # noqa: BLE001 - offline, logged out or an older backend: skip the line
            return
        self._say(f"Learning sharing: {'ON' if got['enabled'] else 'off'} "
                  "(pool coarse records with the other orgs in your company only; "
                  "`brindle account org learning-share`)")

    def cmd_login(self, base: str | None, device: bool = False) -> int:
        """Browser sign-in when a browser can open here (a terminal, not SSH,
        a display on Linux), else, or with ``--device``, the device code."""
        client = auth.Client(base, transport=self.transport)
        browser = not device and loopback.can_open_browser(self.out)
        ent = auth.login(client, self.store, show=self._say, browser=browser)
        self._say(f"Logged in as {ent.sub} ({ent.org_id}), plan {ent.plan}.")
        self._say("See what your plan includes: `brindle account`"
                  + ("" if ent.features else "; get brindle Pro: `brindle account upgrade`"))
        return 0

    def cmd_logout(self, base: str | None) -> int:
        try:
            client = self._client(base)
        except auth.AuthError:
            client = None
        auth.logout(client, self.store)
        self._say("Logged out of brindle Pro.")
        return 0

    def cmd_status(self, base: str | None) -> int:
        client = self._client(base)
        try:
            ent = license.current(store=self.store, client=client)
        except license.LicenseError as e:
            self._say(f"brindle Pro: {e}")
            return 1
        try:
            who = auth.me(client, self.store)
        except auth.AuthError as e:
            who = {}
            if e.code == "airgap":
                self._say("(air-gap mode: showing the offline license)")
            elif e.code == "transport":
                self._say("(offline: showing the stored entitlement)")
            else:
                self._say(f"(could not load account details: {e.code})")
        now = time.time()
        state = "offline grace" if ent.in_grace else ent.status
        self._say(f"brindle Pro: {state}")
        self._say(f"  account   {who.get('email') or ent.sub}")
        self._say(f"  org       {ent.org_id}" + (f" ({ent.role})" if ent.role else ""))
        self._say(f"  plan      {ent.plan} ({ent.seats} seat(s))")
        if who.get("plan") and who.get("plan") != ent.plan:
            self._say(f"            (account now shows plan {who['plan']}; "
                      "the entitlement updates on its next refresh)")
        self._say(f"  features  {', '.join(sorted(ent.features)) or '-'}")
        cloud = "learning" in ent.features and not ent.in_grace
        self._say(f"  learning  {'cloud (hosted learning active)' if cloud else 'off (no hosted learning)'}"
                  + ("" if cloud or 'learning' not in ent.features
                     else " -- offline; hosted learning resumes after a refresh"))
        self._say(f"  expires   {_when(ent.exp)}")
        if ent.in_grace:
            left = max(0, int((ent.grace_until or now) - now)) // 3600
            self._say(f"  grace     until {_when(ent.grace_until)} (~{left} h); "
                      "reconnect to refresh")
        self._airgap_lines()
        return 0

    # -- the offline license ----------------------------------------------------------------

    def _airgap_lines(self) -> None:
        from brindle import airgap

        if not airgap.enabled():
            return
        self._say(f"  air-gap   on ({airgap.source()}): no outbound traffic, local models only")
        warning = airgap.warning()
        if warning:
            self._say(f"            ! {warning}")

    def cmd_license_install(self, base: str | None, path: str) -> int:
        from pathlib import Path

        try:
            data = Path(path).read_bytes()
        except OSError as e:
            raise license.LicenseError(f"cannot read {path}: {e.strerror or e}") from e
        if len(data) > license.MAX_LICENSE_FILE:
            raise license.LicenseError(f"{path} is too large to be a license file")
        ent = license.install(data)
        self._say(f"Installed offline license for org {ent.org_id} (plan {ent.plan}, "
                  f"{ent.seats} seat(s)); expires {_when(ent.exp)}.")
        self._say(f"  features  {', '.join(sorted(ent.features)) or '-'}")
        from brindle import airgap

        if airgap.FEATURE in ent.features:
            self._say('  air-gap mode is included: turn it on with "airgap": true in '
                      f".brindle/config.json or {airgap.ENV}=1")
        else:
            self._say(f"  this license doesn't include air-gap mode ({airgap.FEATURE!r})")
        return 0

    def cmd_license_status(self, base: str | None) -> int:
        try:
            ent = license.installed()
        except license.LicenseError as e:
            self._say(f"offline license: {e}")
            self._airgap_lines()
            return 1
        if ent is None:
            self._say("No offline license installed (`brindle account license install <file>`).")
            self._airgap_lines()
            return 1
        state = "offline grace" if ent.in_grace else ent.status
        self._say(f"offline license: {state}")
        self._say(f"  org       {ent.org_id}")
        self._say(f"  plan      {ent.plan} ({ent.seats} seat(s))")
        self._say(f"  features  {', '.join(sorted(ent.features)) or '-'}")
        self._say(f"  expires   {_when(ent.exp)}")
        if ent.in_grace:
            self._say(f"  grace     until {_when(ent.grace_until)}; install a renewed license")
        self._airgap_lines()
        return 0

    def cmd_license_remove(self, base: str | None) -> int:
        self._say("Removed the offline license." if license.uninstall()
                  else "No offline license was installed.")
        return 0

    def cmd_upgrade(self, base: str | None, team: bool = False, seats: int | None = None,
                    org: str | None = None, enterprise: bool = False, trial: bool = False) -> int:
        if enterprise:
            if trial and seats is not None and seats > TRIAL_MAX_SEATS:
                raise auth.AuthError(f"an Enterprise trial is limited to {TRIAL_MAX_SEATS} seats "
                                     f"(you asked for {seats}): lower --seats, or drop --trial",
                                     code="bad_request")
            self._open(auth.checkout_url(self._client(base), self.store, plan="enterprise",
                                        seats=seats, org_id=self._team_org(org), trial=trial))
            return 0
        if not team:
            self._open(auth.checkout_url(self._client(base), self.store))
            return 0
        self._open(auth.checkout_url(self._client(base), self.store, plan="team", seats=seats,
                                    org_id=self._team_org(org)))
        return 0

    def cmd_portal(self, base: str | None, org: str | None = None) -> int:
        if org is not None and not auth.ORG_ID_RE.match(org):
            raise auth.AuthError("invalid org id", code="bad_request")
        org = org or (self.store.load() or {}).get("org_id")
        self._open(auth.portal_url(self._client(base), self.store, org))
        return 0

    def cmd_seats(self, base: str | None, n: str, org: str | None = None) -> int:
        if not (n.isascii() and n.isdigit() and int(n) >= 1):
            raise auth.AuthError("seats must be a whole number, at least 1", code="bad_request")
        seats = int(n)
        org_id = self._team_org(org)
        status, body = auth.set_seats(self._client(base), self.store, org_id, seats)
        if status != 200:
            raise _seats_error(status, body, seats)
        now = body.get("seats") if isinstance(body.get("seats"), int) else seats
        used = body.get("used")
        if isinstance(used, int):
            self._say(f"Seats: {now} ({used} used). Stripe prorates the change on your next invoice.")
        else:
            self._say(f"Seats: {now}. Stripe prorates the change on your next invoice.")
        return 0


def _seats_error(status: int, body: dict, seats: int) -> auth.AuthError:
    """The message for a refused ``POST /orgs/{org_id}/seats``."""
    code = body.get("error") if isinstance(body.get("error"), str) else ""
    desc = auth._sanitize(body.get("error_description")) \
        if isinstance(body.get("error_description"), str) else ""
    if status == 403:
        return auth.AuthError("you need billing rights in this org to change its seats "
                              "(an owner or billing admin)", code="forbidden")
    if status == 409 and code == "below_used":
        return auth.AuthError(f"{seats} is below the members using seats in this org; "
                              "deactivate someone first, then retry", code="below_used")
    if status == 409 and code == "over_max":
        return auth.AuthError(desc or "the seat cap for this plan was reached", code="over_max")
    if status == 409 and code == "no_subscription":
        return auth.AuthError("this org has no Team or Enterprise subscription; see "
                              "`brindle account upgrade`", code="no_subscription")
    if status == 400:
        return auth.AuthError("seats must be a whole number, at least 1", code="bad_request")
    return auth._error(status, body)


def make(repo_root: str | None = None) -> ProAccount:
    return ProAccount(repo_root)
