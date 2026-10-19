// brindle web: polls /api/snapshot and draws workspaces, agents and the goal.
// Everything from the server is set as text (never as HTML).
"use strict";

const POLL_MS = 2000;
let selectedWorkspace = null;
let diffRequest = 0;
let lastDiff = "";

// The token came in the URL once; the server has set a cookie, so drop it
// from the address bar and history.
if (location.search.includes("token=")) {
  history.replaceState(null, "", location.pathname);
}

function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === "class") node.className = v;
    else node.setAttribute(k, v);
  }
  for (const c of children) {
    if (c === null || c === undefined || c === "") continue;
    node.append(c instanceof Node ? c : String(c));
  }
  return node;
}

function statusClass(status) {
  if (!status) return "";
  if (status === "processing" || status === "working") return "s-busy";
  if (status === "waiting" || status === "blocked" || status.startsWith("locked")) return "s-alert";
  if (status === "done" || status === "passed") return "s-ok";
  if (status === "failed" || status === "exited") return "s-bad";
  return "";
}

function agentRow(a) {
  return el("div", { class: "agent" },
    el("span", { class: "mono" }, a.id),
    el("span", { class: "dim" }, a.profile),
    el("span", { class: "status " + statusClass(a.status) }, a.status),
    a.unread ? el("span", { class: "s-alert" }, a.unread + " unread") : null,
    a.tokens ? el("span", { class: "dim" }, a.tokens) : null);
}

function workspaceBlock(ws) {
  const git = [];
  if (ws.ahead) git.push("↑" + ws.ahead);
  if (ws.behind) git.push("↓" + ws.behind);
  if (ws.dirty) git.push(ws.dirty + " dirty");
  const block = el("div", { class: "ws", "data-workspace": ws.id },
    el("div", { class: "ws-head" },
      workspaceButton(ws),
      el("span", { class: "mono dim" }, ws.branch || ""),
      ws.base_branch ? el("span", { class: "dim" }, "→ " + ws.base_branch) : null,
      git.length ? el("span", { class: "dim" }, git.join(" ")) : null,
      ws.review_state ? el("span", { class: "dim" }, ws.review_state) : null));
  for (const a of ws.agents) block.append(agentRow(a));
  return block;
}

function workspaceButton(ws) {
  const button = el("button", { class: "ws-select", type: "button",
    "aria-pressed": String(selectedWorkspace === ws.id) }, ws.name);
  button.addEventListener("click", () => {
    selectedWorkspace = ws.id;
    document.querySelectorAll(".ws-select").forEach(b => b.setAttribute("aria-pressed", "false"));
    button.setAttribute("aria-pressed", "true");
    loadDiff();
  });
  return button;
}

function diffBlock(data) {
  const commit = sha => (sha || "").slice(0, 8) + (sha === data.sha ? " · HEAD" : " · older commit");
  const reviews = el("div", {}, el("h3", {}, "Reviews"));
  for (const r of data.reviews) {
    reviews.append(el("p", {},
      el("span", { class: "status " + (r.approved ? "s-ok" : "s-bad") },
        r.approved ? "Approved" : "Changes requested"),
      " · " + (r.reviewer_id || "reviewer") + " · " + commit(r.sha),
      r.summary ? el("pre", { class: "mono" }, r.summary) : null));
  }
  if (!data.reviews.length) reviews.append(el("p", { class: "dim" }, "No reviews recorded."));
  const checks = el("div", {}, el("h3", {}, "Checks"));
  for (const c of data.checks) {
    checks.append(el("details", {}, el("summary", {},
      el("span", { class: "status " + (c.ok ? "s-ok" : "s-bad") }, c.ok ? "Passed" : "Failed"),
      " · " + c.command + " · " + commit(c.sha)),
      el("pre", { class: "mono" }, c.output || "No output.")));
  }
  if (!data.checks.length) checks.append(el("p", { class: "dim" }, "No checks recorded."));
  return el("div", {},
    el("p", {}, el("strong", {}, data.name), " → " + data.base),
    el("pre", { class: "mono" }, data.stat || "No changes."),
    data.stat_truncated ? el("p", { class: "dim" }, "Stat truncated.") : null,
    el("details", { "data-diff-patch": "" }, el("summary", {}, "Full diff"),
      el("pre", { class: "mono" }, data.diff || "No changes."),
      data.truncated ? el("p", { class: "dim" }, "Diff truncated; inspect the workspace for the rest.") : null),
    el("p", { class: "dim" }, "Reviews and checks apply to the recorded commit; uncommitted edits have not been verified."),
    reviews, checks);
}

async function loadDiff(refresh = false) {
  const panel = document.getElementById("diff");
  const workspace = selectedWorkspace;
  const serial = ++diffRequest;
  const expanded = refresh && panel.querySelector("[data-diff-patch]")?.open;
  if (!refresh) {
    panel.textContent = "Loading…";
    lastDiff = "";
  }
  try {
    const resp = await fetch("api/workspaces/" + encodeURIComponent(workspace) + "/diff",
      { credentials: "same-origin" });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || "HTTP " + resp.status);
    if (serial === diffRequest && JSON.stringify(data) !== lastDiff) {
      lastDiff = JSON.stringify(data);
      panel.replaceChildren(diffBlock(data));
      if (expanded) panel.querySelector("[data-diff-patch]").open = true;
    }
  } catch (e) {
    if (serial === diffRequest) {
      lastDiff = "";
      panel.textContent = String(e.message || e);
    }
  }
}

function goalBlock(pilot) {
  if (!pilot) return el("p", { class: "dim" }, "No goal for this session.");
  const list = el("ol", { class: "milestones" });
  for (const m of pilot.milestones) {
    const item = el("li", {},
      el("span", { class: "status " + statusClass(m.status) }, m.status), " ",
      m.title,
      m.checked_at ? el("span", { class: "dim" },
        " · checked " + new Date(m.checked_at * 1000).toLocaleTimeString()) : null);
    if (m.check || m.output) {
      item.append(el("details", {},
        el("summary", { class: "dim mono" }, m.check || "output"),
        m.output ? el("pre", { class: "mono" }, m.output) : null));
    }
    list.append(item);
  }
  return el("div", {},
    el("p", {}, el("strong", {}, pilot.goal || "No goal set yet."),
      " ", el("span", { class: "dim" }, pilot.enabled ? "autopilot " + pilot.state : "autopilot off")),
    pilot.note ? el("p", { class: "dim" }, pilot.note) : null,
    list);
}

function render(snap) {
  document.getElementById("repo").textContent = snap.repo_root || "all repos";
  const wsBox = document.getElementById("workspaces");
  wsBox.replaceChildren(...(snap.workspaces.length
    ? snap.workspaces.map(workspaceBlock)
    : [el("p", { class: "dim" }, "No workspaces.")]));
  document.getElementById("goal").replaceChildren(goalBlock(snap.autopilot));
  document.getElementById("updated").textContent =
    "updated " + new Date(snap.now * 1000).toLocaleTimeString();
}

async function poll() {
  const error = document.getElementById("error");
  try {
    const resp = await fetch("api/snapshot", { credentials: "same-origin" });
    if (!resp.ok) throw new Error(resp.status === 403 ? "not authorized: reopen the link brindle printed" : "HTTP " + resp.status);
    render(await resp.json());
    if (selectedWorkspace) await loadDiff(true);
    error.textContent = "";
  } catch (e) {
    error.textContent = String(e.message || e);
  }
  setTimeout(poll, POLL_MS);
}

poll();
