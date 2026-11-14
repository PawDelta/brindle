"""A restarted supervisor's seed keeps track of the work: what it did with its
own tools, the latest messages whole, and where each open branch stands now.
Each test is a failure seen in a live restart."""

import json
from dataclasses import replace

from brindle import bus, supervisor_state
from fast_repo import repo, repo_template  # noqa: F401
from test_efficiency_supervisor import session  # noqa: F401
from test_merge_order import add


def chat(tmp_path, entries) -> str:
    path = tmp_path / "chat.jsonl"
    path.write_text("".join(json.dumps(e) + "\n" for e in entries))
    return path


def said(who: str, text: str) -> dict:
    kind = "assistant" if who == "you" else "user"
    return {"type": kind, "message": {"role": kind, "content": [{"type": "text", "text": text}]}}


def ran(call_id: str, command: str, output: str, failed: bool = False) -> list[dict]:
    return [
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": call_id, "name": "Bash", "input": {"command": command}}]}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": call_id, "content": output, "is_error": failed}]}},
    ]


def test_seed_carries_the_supervisors_own_pushes_and_prs(db, session, tmp_path):
    session(supervisor_state=True)
    path = chat(tmp_path, [
        said("message", "Open the PR for the restart fix."),
        *ran("t1", "git push origin brindle/session-18:fix/restart-pr 2>&1 | tail -3",
             "To github.com:o/r.git\n * [new branch] brindle/session-18 -> fix/restart-pr"),
        *ran("t2", 'gh pr create --head fix/restart-pr --base main --title "Restart seed fix" --body ""',
             "https://github.com/o/r/pull/412\n"),
        said("you", "Opened it. I'm watching CI on #412 and will merge when it is green."),
        # Failed actions must not read as done, nor move the PR head.
        *ran("t3", "gh pr merge 412 --merge", "X Pull request #412 is not mergeable: checks failed", failed=True),
        *ran("t4", "git push origin HEAD:fix/other", " ! [rejected] HEAD -> fix/other (fetch first)", failed=True),
        *ran("t5", 'gh pr create --base main --title "Follow-up"', "https://github.com/o/r/pull/413\n"),
        # Nor do commands a compound line skipped, or a link another command printed.
        *ran("t6", "true || gh pr merge 413 --merge", ""),
        *ran("t7", "gh pr create --head fix/one && false && gh pr create --head fix/two",
             "https://github.com/o/r/pull/414\n", failed=True),
        *ran("t8", 'printf "https://github.com/o/r/pull/415\\n" && false && gh pr create --head fix/three',
             "https://github.com/o/r/pull/415\n", failed=True),
        said("message", "[brindle] w2 finished"),
    ])
    seed = supervisor_state.seed_prompt(db, "boss", path)
    assert "opened PR #412 (head fix/restart-pr, base main, repo o/r)" in seed
    assert "pushed: git push origin brindle/session-18:fix/restart-pr" in seed
    assert "you said: I'm watching CI on #412 and will merge when it is green." in seed
    assert "merged: gh pr merge 412" not in seed and "failed: gh pr merge 412 --merge" in seed
    assert "pushed: git push origin HEAD:fix/other" not in seed
    assert "opened PR #413 (head: your current branch (last pushed: fix/restart-pr)" in seed
    assert "merged: gh pr merge 413" not in seed
    assert "head fix/two" in seed and "opened PR #414 (head fix/two" not in seed
    assert "head fix/three" in seed and "opened PR #415" not in seed


def test_seed_keeps_latest_messages_whole_and_the_persons_newest(db, session, tmp_path):
    session(supervisor_state=True)
    request = "Please change the restart seed. " + "Here is the detail of it. " * 120 + "REQUEST-ENDS-HERE"
    answer = "Here is what I found. " + "One finding after another. " * 80 + "ANSWER-ENDS-HERE"
    path = chat(tmp_path, [
        *[said("message", f"older-{i} " + "filler " * 50) for i in range(20)],
        # The app delivers the person's message under brindle's delivery line too.
        said("message", "brindle delivered this message:\n" + request),
        *[said("message", (f"brindle delivered this message from developer agent w{i}:\n" if i % 2 else
                           "brindle delivered this message:\n[brindle] ") + f"delivery-{i} " + "worker report " * 120)
          for i in range(10)],
        said("you", answer),
    ])
    seed = supervisor_state.seed_prompt(db, "boss", path)
    assert len(seed.encode()) <= supervisor_state.SEED_BYTES
    latest = seed.split("## Latest messages")[1]
    assert request in latest and answer in latest
    # Older messages give way first.
    assert "delivery-9" in latest and "delivery-0 " not in latest and "older-0 " not in latest


def test_seed_shows_an_open_branch_as_its_worker_row_stands(db, session, tmp_path):
    session(supervisor_state=True)
    root_ws = db.get_workspace(db.get_agent("boss").workspace_id)
    ws = replace(root_ws, id="ws-plan", name="feat/plan", branch="feat/plan",
                 path=str(tmp_path / "feat-plan"), kind="worktree")
    db.add_workspace(ws)
    add(db, ws, "w-plan", "assign", parent="boss", result="First pass done.")
    bus.publish_agent(db, "pipeline.reported", "w-plan", branch="feat/plan")
    # Sent more work since: plan-first, its plan waits on the supervisor.
    db.update_agent("w-plan", plan_first=1, plan_state="proposed", plan_text="Split the module.")
    seed = supervisor_state.seed_prompt(db, "boss")
    assert "feat/plan (w-plan): waiting for your plan approval" in seed.split("## Branches")[1]
