"""Settings' one-row editor over the CLI's analysis-doc policy."""
from brindle.daemon.routes_actions import _docs_entitlement, docs_org_api
from brindle.pro import analysis_policy
from brindle.service.errors import ServiceError


def read(repo=None):
    ent = _docs_entitlement(repo)
    policy = docs_org_api("GET", "policy", repo=repo, admin=True, org_id=ent.org_id)
    analysis_policy.save_cached(ent.org_id, policy)
    return {"org_id": ent.org_id, "actor_sub": ent.sub, "policy": policy,
            "can_edit_org": "analysis_docs_enterprise" in ent.features,
            "can_edit_groups": "analysis_docs_team" in ent.features}


def save(body):
    from brindle.daemon.routes_actions import _get

    org = _get(body, "org_id", required=True)
    actor = _get(body, "actor_sub", required=True)
    repo = _get(body, "repo")
    ent = _docs_entitlement(repo)
    if ent.role not in ("owner", "admin"):
        raise ServiceError("forbidden", "only an organisation owner or admin can do this", 403)
    if ent.org_id != org or ent.sub != actor:
        raise ServiceError("identity_changed", "Your active account changed; reload these settings.", 409)
    name = _get(body, "name", required=True)
    value = _get(body, "value", required=True)
    group = _get(body, "group")
    if (group is None and "analysis_docs_enterprise" not in ent.features) or (
            group is not None and "analysis_docs_team" not in ent.features):
        raise ServiceError("forbidden", "Org-wide settings require Enterprise; Team admins can edit group settings.", 403)
    allowed = ("anyone", "org", "off") if name == "share_links" else analysis_policy.STATES
    if name not in (*analysis_policy.SWITCHES, "share_links") or value not in allowed:
        raise ServiceError("invalid", "Unknown organisation setting or value.")
    return _save_row(ent, repo, org, actor, name, value, group, _get(body, "previous", required=True))


def _save_row(ent, repo, org, actor, name, value, group, previous):
    for attempt in range(2):
        current = read(repo)["policy"]
        policy = _changed_policy(current, ent, name, value, group, previous)
        try:
            saved = docs_org_api("PUT", "policy", policy, repo=repo, admin=True, org_id=org)
        except ServiceError as error:
            if error.code == "version_conflict" and attempt == 0:
                continue
            raise
        analysis_policy.save_cached(org, saved)
        return {"org_id": org, "actor_sub": actor, "policy": saved,
                "can_edit_org": "analysis_docs_enterprise" in ent.features,
                "can_edit_groups": "analysis_docs_team" in ent.features}


def _changed_policy(current, ent, name, value, group, previous):
    switches = current["switches"]
    if group is not None:
        if name == "share_links" or group not in current["groups"]:
            raise ServiceError("invalid", "Choose an existing group and a group setting.")
        if current["switches"].get(name) in ("enforced_on", "enforced_off") and "analysis_docs_enterprise" in ent.features:
            raise ServiceError("forbidden", "This setting is fixed for the whole organisation.", 403)
        switches = current["groups"][group]["switches"]
    # Refuse an edit of an out-of-date row, preserving unrelated fields and groups.
    actual = current.get("share_links", "org") if name == "share_links" else switches.get(name, "off")
    if previous != actual:
        raise ServiceError("policy_changed", "This setting changed; reload before saving.", 409)
    policy = {k: v for k, v in current.items() if k not in ("version", "updated_at", "updated_by")}
    policy["switches"] = dict(current["switches"])
    if name == "share_links":
        policy[name] = value
    elif group is None:
        policy["switches"][name] = value
    else:
        policy["groups"] = {**current["groups"], group: {**current["groups"][group], "switches": {**switches, name: value}}}
        if "analysis_docs_enterprise" not in ent.features:
            # Team's endpoint accepts only group policy. Other fields are Enterprise-only.
            policy = {"groups": policy["groups"]}
    return policy
