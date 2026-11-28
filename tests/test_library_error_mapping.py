"""The daemon's Library errors keep machine codes and hide upstream detail."""

import pytest

from brindle.pro import analysis_docs as ad
from brindle.pro.auth import AuthError, TransportError
from pro_fixtures import backend, pro_env, signing_key  # noqa: F401
from test_pro_remote_learning import login_as
from test_shared_plan_docs import BASE, TEAM_LOGIN
from test_shared_reader import CONTENT, DESIGN, DOC, ROUTE, app, lib  # noqa: F401

LINK_TOKEN = "dsl_" + "A" * 43
LINK_ROUTE = "/v1/share-links/design"
LIBRARY_LINK = f"/share-links/{LINK_TOKEN}/design"
ORG_POLICY = "/orgs/org_1/analysis-docs/policy"
PRIVATE_URL = "https://example.test/private-plan"


@pytest.mark.parametrize(
    "route, library_route, reply, expected, hidden",
    [
        pytest.param(f"/v1/library/designs/{DOC}/people", f"{DESIGN}/people",
                     (500, {"error": "internal_error"}), (502, "internal_error"), "internal_error", id="people",
                     marks=pytest.mark.variant("People search keeps the server code on a Library failure.")),
        pytest.param(f"/v1/library/designs/{DOC}/access", BASE,
                     (403, {"error": "forbidden"}), (403, "forbidden"), "forbidden", id="access",
                     marks=pytest.mark.variant("The access list keeps the permission status and hides the raw code.")),
        pytest.param(ROUTE, DESIGN, (200, {**CONTENT, "rev": 1, "access_request": {"role": PRIVATE_URL}}),
                     (502, "upstream"), PRIVATE_URL, id="open"),
        pytest.param(LINK_ROUTE, LIBRARY_LINK,
                     (500, {"error": "internal_error", "error_description": PRIVATE_URL}),
                     (502, "internal_error"), PRIVATE_URL, id="share-link"),
        pytest.param(LINK_ROUTE, LIBRARY_LINK, TransportError(PRIVATE_URL, network="offline"),
                     (503, "library_offline"), PRIVATE_URL, id="offline-link",
                     marks=pytest.mark.variant("Opening a link must never reuse raw transport text.")),
        pytest.param(ROUTE, DESIGN, AuthError(PRIVATE_URL, code="bad_response"),
                     (502, "bad_response"), PRIVATE_URL, id="malformed-sign-in-response"),
        pytest.param(f"/v1/library/designs/{DOC}/share-link", ORG_POLICY, (500, {}),
                     (502, "upstream"), "HTTP 500", id="enterprise-policy"),
        pytest.param(f"/v1/library/designs/{DOC}/share-link", f"{DESIGN}/share-link",
                     (403, {"error": "share_links_off", "error_description": PRIVATE_URL}),
                     (403, "share_links_off"), PRIVATE_URL, id="share-link-policy-refusal"),
    ],
)
def test_library_route_errors(app, lib, route, library_route, reply, expected, hidden):
    if library_route == ORG_POLICY:
        _, store = ad._session()
        login_as(lib.calls, store, features=(*TEAM_LOGIN, "analysis_docs_enterprise"))
    lib.calls.routes[f"GET {library_route}"] = [reply]
    if route == LINK_ROUTE:
        response = app.post(route, json={"token": LINK_TOKEN})
    else:
        response = app.get(route)
    error = response.json()["error"]
    assert (response.status_code, error["code"]) == expected
    assert hidden not in error["message"]
