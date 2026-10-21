# Agents and routing

When brindle gives out a task, it decides which agent does it. This page
explains how that choice is made, how to make your own agents, how rules shape
what an agent does, and how to keep the choice in your hands.

## How a task gets its agent

The supervisor sizes each task as a **kind of work**:

| Kind | Meant for | Default agent |
|---|---|---|
| `light` | small, well-specified, mechanical work (docs, renames, simple tests) | `developer-light` (Claude Haiku, low effort) |
| `medium` | a normal feature or bugfix in one area | `developer` (your Claude Code default model) |
| `heavy` | architecture, broad refactors, subtle bugs | `developer-heavy` (Claude Opus, high effort) |
| `advanced` | complex, long-running system design and implementation | `developer-fable` (Claude Fable, high effort) |
| reviewer | checking a worker's branch before it merges | `reviewer` (the `reviewer` config key) |
| supervisor | planning, handing out tasks, merging | your Claude Code model, or the CLI you pick with `brindle -s` |

Naming an agent always wins: a task given an `agent_profile`, or a milestone
with a `profile`, skips routing entirely.

### The fallback order

For each of `light`, `medium`, `heavy` and `advanced`, the `routing` setting
lists agents in the order brindle tries them. These are the defaults:

```json
{
  "routing": {
    "light":    ["developer-light", "developer", "developer-codex", "developer-local"],
    "medium":   ["developer", "developer-codex", "developer-heavy"],
    "heavy":    ["developer-heavy", "developer-gemini", "developer", "developer-codex"],
    "advanced": ["developer-fable", "developer-heavy", "developer-gemini"]
  }
}
```

An agent is skipped when its CLI isn't installed or signed in, a local model
server isn't answering, or its provider is at your `usage_limit` (90% of your
usage limit by default; `limit_cooldown_minutes` sets how long a provider that
hit its limit counts as unavailable). The first agent left runs. If every
candidate is out, the repo's `default_agent` runs. The reply to the supervisor
says what was picked and why, for example
`weight medium -> developer (Codex at 93%, skipped developer-codex)`.

A single agent can also name its own backup with `fallback:` in its profile
(for example `developer-local` falls back to `developer-light` when its local
model isn't available).

To change the order, set a tier in `.brindle/config.json` (shared with your
team), `.brindle/config.local.json` (just you, this repo) or
`~/.brindle/config.json` (every repo). A tier you set replaces that tier's list;
tiers you leave out keep theirs:

```json
{"routing": {"light": ["developer-codex", "developer"]}}
```

`brindle doctor` shows the list in effect for each tier and the file it came
from.

## Profiles (custom agents)

A **profile** is a markdown file that defines an agent: a model (and effort),
the instructions it gets, and the rules it follows. brindle looks for profiles
in `.brindle/agents/` (this repo), then `~/.brindle/agents/` (you), then your
org's library, then its built-ins.

```markdown
---
name: frontend
description: React/TypeScript specialist
extends: developer
model: sonnet
effort: medium
rules: style/minimal-diff
---
You are a frontend engineer. Prefer the project's existing components...
```

`extends` starts from another profile and overrides only the fields you set;
your instructions are added after the parent's. `provider` can be `claude`,
`codex`, `antigravity`, `opencode`, `native` (local models) and others; see the
README's "Agent profiles" section for every field.

### Make one from the command line

```sh
brindle profile new frontend --extends developer --rules style/minimal-diff \
  --description "React/TypeScript specialist"
```

This writes `.brindle/agents/frontend.md` (add `--user` to write to
`~/.brindle/agents/` instead). Edit the file to set the model, effort and
instructions. Then:

```sh
brindle profile show frontend   # the profile as brindle resolves it
brindle profile lint            # check every profile: parents, packs, patterns
```

### Make one in the desktop app

Open the **Agents** tab and choose **Make a custom agent**. Pick the model and
effort, write its instructions, add rules, and choose **Use it for**:

- **Any kind of work**: the agent is one more choice brindle may route to.
- **A specific kind** (light, medium, heavy, advanced or reviewer): the agent
  takes that kind of work, ahead of the built-in one.

In config terms, choosing a kind puts your agent first in that kind's `routing`
list (or sets `reviewer`); "any kind" adds it as a candidate. You can do the
same by hand:

```json
{"routing": {"medium": ["frontend", "developer", "developer-codex"]}}
```

```json
{"reviewer": "backend-auditor"}
```

## Rules and rule packs

A **rule** is a short instruction added to an agent's brief, such as "Make the
smallest change that completes the task." Rules are grouped in **rule packs**,
and a profile turns them on with `rules:` (a comma-separated list). A pack's
text goes into the agent's prompt for every provider.

Built-in packs:

| Pack | What it asks |
|---|---|
| `security/backend` | no shell-outs from input, no `eval`, no unsafe deserializing, no secrets in code |
| `tests/only` | change tests, not the code under test; every diff must touch a test file |
| `style/minimal-diff` | the smallest diff that does the job; no drive-by cleanups |

A pack can also carry checks brindle runs on the branch before it merges, so a
broken rule sends the branch back to the worker like a failed check:

```markdown
---
name: security/backend
description: Backend code handles untrusted input, secrets and the shell carefully
deny_deps: [pickle]              # names that may not be imported or added to a manifest
require_tests_for: [src/**/*.py] # a change here must come with a test file in the diff
deny_patterns:                   # regexes no added line may match
  - shell=True
---
Treat every input that crosses a trust boundary as hostile until validated...
```

### Add your own

Save a pack as `.brindle/rules/<name>.md` (shared with the repo) or
`~/.brindle/rules/<name>.md` (yours); `team/api.md` is used as `team/api`. Then
name it in a profile's `rules:` line. In the desktop app you can add a rule
from **Knowledge**. A pack a profile names but that can't be found stops the
launch, so a rule never silently stops applying; `brindle profile lint`
catches this earlier.

Rules for every supervisor in a repo go in the `rules` config key:

```json
{"rules": ["Fix review findings without asking"]}
```

With brindle Pro, when reviewers keep asking for the same change,
`brindle rules suggest` offers it as a rule, `brindle rules accept ID` adds it
to `.brindle/rules/learned.md` (a pack every profile in the repo picks up), and
`brindle rules reject ID` dismisses it for good.

## Hosted learning can change the choice

With a plan that includes hosted learning (brindle Pro and Team), brindle can
choose among the candidates in a kind's `routing` list instead of always taking
the first. That means **the profile used for a kind of work can differ from the
default**; the reply to the supervisor says `learning picked it` when it did.

Your choices are never overridden. To keep a choice fixed:

- **Name the agent.** A task's `agent_profile` or a milestone's `profile`
  always wins over learning.
- **List one agent for the kind.** Learning only chooses between two or more
  candidates, so `{"routing": {"heavy": ["developer-heavy"]}}` pins that kind.
- **Turn learning off** with `"learning": "off"` in `.brindle/config.json` or
  `~/.brindle/config.json`.

## Org policy (Enterprise)

An organization can limit which providers and models people may use. Its
policy's `allowed_providers`, `allowed_models` and `allowed_profiles` decide
what the picker (`brindle -s`), the workers screen and the desktop app's model
lists show; anything else doesn't appear, naming it is refused with the reason,
and saved picks are re-checked at every launch. Enterprise orgs can also pin the
route workers run on and disallow personal API keys. See `brindle account org
policy` for the policy that applies to you, and "Managed models" in the README
for the details.
