# README and guide audit against brindle 0.6.5

Scope: claims that can be checked against this checkout's source, configuration, and recorded repository decisions. Line numbers below refer to the audited files before this audit was added. Historical figures are treated as dated snapshots, not current live totals.

## Findings

| Location | Claim | What is true | Code evidence |
|---|---|---|---|
| `docs/guide.md:745`, `docs/guide.md:771` | The sample config sets `max_agents` to 4 and the config table calls 4 the default. | The default is 5 workers per session. The sample should either show 5 or identify 4 as an override. | `src/brindle/config.py:124` (`RepoConfig.max_agents = 5`); `src/brindle/autopilot.py:2629-2637` applies that cap. |
| `docs/guide.md:764` | The default `reviewer` value is the profile named `reviewer`. | The actual `RepoConfig.reviewer` default is `reviewer-sonnet-high`; that is the default profile unless a setting or hosted learner selects otherwise. | `src/brindle/config.py:112`; `src/brindle/agents.py:4836-4853`. |
| `docs/guide.md:758` | An unset `scoped_checks` acts as `off`; the guide says it will become `infer` next release and brindle currently warns users to opt in. | `None` currently acts as `infer`. The worker check scope selects affected tests when graph data is available, with its documented fallback behavior; this is already the runtime default. | `src/brindle/config.py:107`; `src/brindle/check_scope.py:1-33, 181-189`. |
| `docs/guide.md:103` | The status bar example is version `0.6.3`. | The audited source/package is 0.6.5, so this illustrative current-version value is stale. | `pyproject.toml:3`; `src/brindle/tmux.py:598` renders `__version__`. |

### Claims checked and consistent

- `README.md:43` says the default worker cap is five; this matches `src/brindle/config.py:124`.
- `README.md:189-190` says there is no client telemetry and identifies the PyPI update check and its opt-out. Current client source has no usage-count sender (`rg` across `src` and `app` finds none); the update check is implemented at `src/brindle/updates.py:23-43` and honors `BRINDLE_NO_UPDATE_CHECK` at `src/brindle/updates.py:23`. The accepted “Anonymous usage counts” decision says client pings ship after 0.6.3, but no such client implementation exists in this 0.6.5 checkout, so the README statement is not stale on code evidence.
- `README.md:97-98` and `docs/guide.md:96-98` identify Claude Code as the supervisor default, consistent with `src/brindle/config.py:608` (`SUPERVISOR_KEY`) and `src/brindle/cli.py` supervisor selection.
- The README's “Built with brindle” numbers at `README.md:97-108` are explicitly attributed to 2026-10-05 through 2026-11-04 and are historical stats, not claims about the current totals. No discrepancy is reported without a reproducible source snapshot.
- The guide's `review_profile` and sizing notes at `docs/guide.md:321-339, 765-769` describe explicit reviewer selection, learned alternatives, and preserving an existing supported pairing during sizing. That is compatible with the accepted “Cross-vendor review” decision: the product does not require a different vendor by default, while org policy can require it.
- No independently verifiable stale command name or flag was found in the README command table (`README.md:145-160`) or the guide command table (`docs/guide.md:401-457`). The CLI registrations are in `src/brindle/cli.py`.

## Guide section destinations

Each level-two (`##`) section in `docs/guide.md` is classified by its primary audience. “User” means product use, setup, or reference content suitable for the website; “contributor” means development or release process that stays in repository docs.

| Section | Destination |
|---|---|
| Install | user |
| Quick start | user |
| Recommended use | user |
| Driving toward a verified goal | user |
| Settings API | user |
| Settings UI | user |
| Commands | user |
| Connect GitHub | user |
| Token usage and history | user |
| Shared knowledge across sessions | user |
| Provider quota | user |
| Repo config: `.brindle/config.json` | user |
| brindle Pro and Team | user |
| Cleanup, merging and workspace config | user |
| Agent profiles | user |
| Open-weight models | user |
| Google Antigravity | user |
| How it works | user |
| Development | contributor |
| License | user |

Nested headings are covered by their parent section. In particular, CI and Releasing remain under Development and therefore stay in docs; product and plan details remain user-facing.
