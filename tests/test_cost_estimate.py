import json
import re
import time

from brindle import cost_estimate as ce
from brindle.db import Task
from brindle.pro import license

RATES = {"input": 3e-6, "output": 15e-6, "cache_write": 3.75e-6, "cache_read": 0.3e-6}


def price(model, input, output, cache_write, cache_read):
    """A stand-in for brindle.pricing.price: one flat rate, None for an unknown model."""
    if model == "mystery":
        return None
    return (input * RATES["input"] + output * RATES["output"]
            + cache_write * RATES["cache_write"] + cache_read * RATES["cache_read"])


def tokens(i, o=0, cw=0, cr=0, model="claude-sonnet"):
    return json.dumps({"input": i, "output": o, "cache_creation": cw, "cache_read": cr,
                       "model": model})


def add_task(db, repo_root, agent_id, branch, weight, profile="developer"):
    db.add_task(Task(f"t-{agent_id}", repo_root, agent_id, "boss", "ws", profile, "do it",
                     "assign", 1, branch, None, None, None, "started", time.time(),
                     weight=weight))


def seed(db, repo_root, n, *, profile="developer", reviewer="reviewer", weight="medium",
         base=100_000, model="claude-sonnet"):
    """n workers, each reviewed once; worker i used base*(i+1) input tokens."""
    for i in range(n):
        w, r, branch = f"w{weight}{i}", f"r{weight}{i}", f"feat/{weight}{i}"
        add_task(db, repo_root, w, branch, weight, profile)
        # two rows for one worker: their deltas add up to the run
        db.add_history(repo_root, "worker_result", agent_id=w, branch=branch, profile=profile,
                       tokens=tokens(base * (i + 1) // 2, 1000 * (i + 1), model=model))
        db.add_history(repo_root, "worker_result", agent_id=w, branch=branch, profile=profile,
                       tokens=tokens(base * (i + 1) // 2, 1000 * (i + 1), model=model))
        db.add_history(repo_root, "review", agent_id=r, branch=branch, profile=reviewer,
                       tokens=tokens(10_000, 500, model=model))
        db.add_history(repo_root, "merge", agent_id="boss", branch=branch,
                       tokens=tokens(999_999, model=model))


def dollars(text):
    return [float(x.replace(",", "")) for x in re.findall(r"\$([\d,]+\.?\d*)", text)]


# -- history -> runs -> distributions ---------------------------------------------------


# -- estimates ----------------------------------------------------------------------------


def test_cheaper_alternative_prefers_a_local_reviewer(db):
    seed(db, "/repo", 6)
    seed(db, "/repo", 6, reviewer="reviewer-codex", weight="light", base=50_000)
    text = ce.describe([ce.TaskSpec("developer", "medium")] * 2, "reviewer", repo_root="/repo",
                       db=db, price=price, candidates=["reviewer", "reviewer-codex", "reviewer-local"])
    first, cheaper = text.splitlines()
    assert "Cheaper: review with reviewer-local instead of reviewer" in cheaper
    assert dollars(cheaper)[1] < dollars(first)[1]


def test_cheaper_alternative_without_local_takes_the_cheapest(db):
    seed(db, "/repo", 6)
    dists = ce.distributions(ce.runs(db, "/repo"))
    tasks = [ce.TaskSpec("developer", "medium")]
    # no history for the others: priced from the prior, which costs more than reviewer's runs here
    assert ce.cheaper(tasks, "reviewer", ["reviewer-codex"], dists=dists, price=price) is None
    # a candidate that is genuinely cheaper wins
    cheap = lambda model, i, o, cw, cr: 0.0 if model == "free" else price(model, i, o, cw, cr)
    for i in range(6):
        db.add_history("/repo", "review", agent_id=f"cx{i}", profile="reviewer-codex",
                       tokens=tokens(1000, model="free"))
    dists = ce.distributions(ce.runs(db, "/repo"))
    name, est = ce.cheaper(tasks, "reviewer", ["reviewer", "reviewer-codex"], dists=dists, price=cheap)
    assert name == "reviewer-codex"


# -- the Pro gate --------------------------------------------------------------------------


def test_reply_note_fails_closed(db, monkeypatch):
    seed(db, "/repo", 6)
    monkeypatch.setattr(license, "has", lambda feature: False)
    assert ce.reply_note(db, "/repo", [ce.TaskSpec("developer")]) is None

    def explode(feature):
        raise RuntimeError("unreadable")

    monkeypatch.setattr(license, "has", explode)
    assert ce.reply_note(db, "/repo", [ce.TaskSpec("developer")]) is None
