"""Folding context asks the model for the summary, and falls back to brindle's own."""

from __future__ import annotations

import json

from brindle.native import LoopConfig
from brindle.native.loop import SUMMARY_SYSTEM

from test_native_loop import agent, fake, openai_reply  # noqa: F401  (fake is a fixture)


def is_summary_request(req: dict) -> bool:
    return SUMMARY_SYSTEM in json.dumps(req["messages"][0])


def script(fake, tmp_path, summaries, reads=12):
    """Twelve reads of a big file then 'done'. Summary requests (recognized by
    their system prompt) get the next of ``summaries``: a text, or an int
    status for an endpoint error; the rest of the conversation is scripted."""
    (tmp_path / "big.txt").write_text("x" * 4000)
    work = [openai_reply(calls=[(f"c{i}", "Read", {"path": "big.txt"})]) for i in range(reads)]
    work.append(openai_reply("done"))
    summaries = list(summaries)
    work_iter = iter(work)

    def route(req):
        if is_summary_request(req):
            s = summaries.pop(0) if summaries else 400
            return s if isinstance(s, int) else openai_reply(s, usage=(7, 3))
        return next(work_iter)

    fake.replies = [route] * (reads + 1 + 20)


def make(fake, tmp_path):
    return agent(fake, tmp_path, config=LoopConfig(context_tokens=2000, keep_recent=4, old_result_chars=100))


def summary_requests(fake):
    return [r for r in fake.requests if is_summary_request(r)]


def test_an_endpoint_error_falls_back_to_the_deterministic_summary(fake, tmp_path):
    script(fake, tmp_path, [400] * 10)  # 400 isn't retried
    notes = tmp_path / "t.jsonl"
    a = agent(fake, tmp_path, config=LoopConfig(context_tokens=2000, keep_recent=4, old_result_chars=100),
              transcript=notes)
    assert a.run("the task") == "done"
    assert summary_requests(fake)
    assert "you ran Read big.txt" in a.messages[1]["content"]
    assert "summarized by brindle" in notes.read_text()


def test_a_second_fold_extends_the_first_summary(fake, tmp_path):
    script(fake, tmp_path, ["first fold notes", "second fold notes"] + ["more notes"] * 10)
    a = make(fake, tmp_path)
    assert a.run("the task") == "done"
    assert len(summary_requests(fake)) >= 2
    summary = a.messages[1]["content"]
    assert summary.count("summarized by brindle") == 1
    assert "first fold notes" in summary and "second fold notes" in summary
    assert summary.index("first fold notes") < summary.index("second fold notes")
    # The second request saw the earlier summary, not the messages it replaced.
    second = summary_requests(fake)[1]["messages"]
    assert "first fold notes" in json.dumps(second)
