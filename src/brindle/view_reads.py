"""Batched reads for one sidebar rendering, sharing its caller's connection.

The view and review helpers keep their usual DB interface, but repeated small
lookups read these per-render indexes. Nothing survives the rendering: writes
and reads not listed here still use DB normally. This is not a connection owner.
"""

import json
import time
from collections import defaultdict
from functools import cached_property
from sqlite3 import Row

from brindle.chat import _QUEUE as chat_queue
from brindle.db import DB, Agent, Autopilot, Review, SessionRepo, Workspace, _load


class ViewReads(DB):
    def __init__(self, db: DB) -> None:
        self.conn = db.conn

    @cached_property
    def _agents(self):
        return {a.id: a for a in super().list_agents()}

    @cached_property
    def _by_workspace(self):
        rows = defaultdict(list)
        for a in self._agents.values():
            rows[a.workspace_id].append(a)
        return rows

    @cached_property
    def _children(self):
        rows = defaultdict(list)
        for a in self._agents.values():
            rows[a.parent_id].append(a)
        return rows

    def list_agents(self, workspace_id: str | None = None) -> list[Agent]:
        return list(self._by_workspace[workspace_id] if workspace_id else self._agents.values())

    def get_agent(self, agent_id: str) -> Agent | None:
        return self._agents.get(agent_id)

    def children(self, parent_id: str) -> list[Agent]:
        return list(self._children[parent_id])

    @cached_property
    def _workspaces(self):
        return {ws.id: ws for ws in super().find_workspaces()}

    def find_workspaces(self, repo_root: str | None = None) -> list[Workspace]:
        return [ws for ws in self._workspaces.values() if not repo_root or ws.repo_root == repo_root]

    def get_workspace(self, workspace_id: str) -> Workspace | None:
        return self._workspaces.get(workspace_id)

    @cached_property
    def _repos(self):
        return super().session_repos()

    def session_repos(self, root_id: str | None = None) -> list[SessionRepo]:
        return [r for r in self._repos if not root_id or r.root_id == root_id]

    @cached_property
    def _milestones(self):
        return dict(self.conn.execute("SELECT agent_id, title FROM agent_milestones"))

    def agent_milestone(self, agent_id: str) -> str | None:
        return self._milestones.get(agent_id)

    @cached_property
    def _messages(self):
        return {r[0]: (r[1], r[2]) for r in self.conn.execute(
            "SELECT agent_id, SUM(COALESCE(held,0)=0), SUM(held=1) FROM inbox "
            "WHERE delivered_at IS NULL GROUP BY agent_id")}

    def pending_count(self, agent_id: str) -> int:
        return self._messages.get(agent_id, (0, 0))[0]

    def unread_count(self, agent_id: str) -> int:
        return self._messages.get(agent_id, (0, 0))[1] or 0

    @cached_property
    def _chat_queues(self):
        return dict(self.conn.execute("SELECT agent_id, value FROM agent_marks WHERE kind=?", (chat_queue,)))

    def mark(self, agent_id: str, kind: str) -> str | None:
        if kind == chat_queue:
            return self._chat_queues.get(agent_id)
        return super().mark(agent_id, kind)

    def set_mark(self, agent_id: str, kind: str, value: str | None) -> None:
        super().set_mark(agent_id, kind, value)
        if kind == chat_queue:
            self.__dict__.pop("_chat_queues", None)

    @cached_property
    def _reviews(self):
        rows = defaultdict(list)
        for r in self.conn.execute(
                "SELECT * FROM reviews WHERE id IN (SELECT MAX(id) FROM reviews "
                "GROUP BY workspace_id, sha, reviewer_id) ORDER BY id"):
            review = _load(Review, r)
            rows[review.workspace_id].append(review)
        return rows

    def last_review(self, workspace_id: str) -> Review | None:
        rows = self._reviews[workspace_id]
        return rows[-1] if rows else None

    def reviews_at(self, workspace_id: str, sha: str) -> list[Review]:
        return [r for r in self._reviews[workspace_id] if r.sha == sha]

    @cached_property
    def _merges(self):
        return dict(self.conn.execute("SELECT workspace_id, sha FROM merges"))

    def merged_sha(self, workspace_id: str) -> str | None:
        return self._merges.get(workspace_id)

    @cached_property
    def _usage(self):
        return {r["path"]: r for r in self.conn.execute("SELECT * FROM usage_cache")}

    def get_usage_cache(self, path: str) -> Row | None:
        return self._usage.get(path)

    @cached_property
    def _autopilots(self):
        rows = self.conn.execute(
            "SELECT a.*, d.offer AS doc_offer, d.goal_at AS doc_goal_at, s.summary AS goal_summary "
            "FROM autopilot a LEFT JOIN goal_doc_requests d ON d.root_id=a.root_id "
            "LEFT JOIN goal_summaries s ON s.root_id=a.root_id")
        result = {}
        for row in rows:
            ap = _load(Autopilot, row)
            ap.enabled = 1
            ap.plan_offer, ap.offer_goal_at = row["doc_offer"], row["doc_goal_at"]
            ap.summary = row["goal_summary"]
            result[ap.root_id] = ap
        return result

    def get_autopilot(self, root_id: str) -> Autopilot | None:
        return self._autopilots.get(root_id)

    def prepare_autopilots(self, root_ids: set[str]) -> None:
        """Backfill legacy rows only for roots this rendering will look up."""
        missing = [root_id for root_id in root_ids if root_id not in self._autopilots
                   and (root := self.get_agent(root_id)) and root.parent_id is None
                   and root.mode == "interactive" and root.status not in ("paused", "done")]
        if missing:
            self.conn.execute(
                "INSERT OR IGNORE INTO autopilot (root_id, enabled, created_at) "
                "SELECT id, 1, ? FROM agents WHERE id IN (SELECT value FROM json_each(?)) "
                "AND parent_id IS NULL AND mode='interactive' AND status NOT IN ('paused', 'done')",
                (time.time(), json.dumps(missing)))
            del self._autopilots
