// brindle web: polls /api/snapshot and draws workspaces, agents and the goal.
// Everything from the server is set as text (never as HTML).
"use strict";

const POLL_MS = 2000;

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
      el("strong", {}, ws.name),
      el("span", { class: "mono dim" }, ws.branch || ""),
      ws.base_branch ? el("span", { class: "dim" }, "→ " + ws.base_branch) : null,
      git.length ? el("span", { class: "dim" }, git.join(" ")) : null,
      ws.review_state ? el("span", { class: "dim" }, ws.review_state) : null));
  for (const a of ws.agents) block.append(agentRow(a));
  return block;
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
    error.textContent = "";
  } catch (e) {
    error.textContent = String(e.message || e);
  }
  setTimeout(poll, POLL_MS);
}

poll();
