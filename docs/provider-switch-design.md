# Provider switch: design

When a session's supervisor runs out of quota on its provider, brindle offers to
carry the session on with another provider in the same workspace and branch, and
offers to switch back once the original limit resets.

**Code:** `src/brindle/provider_switch.py` (stubs), table `provider_switches` (db.py),
route `POST /v1/sessions/{root}/provider-switch` (routes_actions.py),
`app/src/components/ProviderSwitch/`. Tests: `tests/test_provider_switch.py`
(`-k offer`, `-k handoff`, `-k switch_back`) and `ProviderSwitch.test.tsx`. All of them fail on the stubs for now.

## Decisions made (open to change)

1. **Out of quota** means `quota.get(p).limited_until` is later than `now`, or the
   fullest plan window (`quota.fullest`) is at 100%, until its `resets_at`. A
   one-model cap (a named window) doesn't count. Claude on an API key is never out
   of quota. Native (local model) is never a source or a target.
2. **Target**: a provider other than the current one that `preflight.check` finds
   installed, turned on and signed in, and that is in quota. The one with the most
   headroom wins; a tie goes in the order claude, codex, antigravity, opencode. If no
   provider qualifies, brindle makes no offer and writes no row. Routing config and
   org policy aren't consulted yet (see open item a).
3. **The person always decides.** brindle never switches on its own: an Inbox card
   (kind `provider_switch`) offers Switch or Not now. A switch changes vendor and
   cost, and brindle stays vendor-neutral by default (decision 4e5e8332).
4. **Same workspace and branch.** `switch` calls `sessions.handover_to` with `dest`
   set to the old root's own workspace, so the goal, milestones, workers, queued
   tasks and attached repos move across as they do in a handover. This needs two
   small changes to `handover_to`. It must take a `provider=` argument (today it reuses
   `old.provider`). It also must not hand the session to a live supervisor in that workspace that
   runs on a different provider than the one asked for.
5. **The old supervisor is paused, not closed** (`pause_old=True`). A switch back
   starts a fresh supervisor on the original provider with a new handoff note. It
   doesn't resume the paused one, which wouldn't know what happened on the other
   provider.
6. **The handoff note comes from brindle's records only.** It contains the goal,
   each milestone with its last result, the workers (id, profile, status and branch),
   queued tasks, the last 10 inbox messages to or from the root, and the text of the
   last 40 turns of the root's transcript file. brindle doesn't read any CLI
   internals beyond the transcript it already records.
7. **One supervisor per press.** A partial unique index allows one `open` or
   `switching` offer per root. Switch claims the offer with a single
   `UPDATE … SET state='switching' WHERE state='open'`. A second press finds the
   offer taken and returns the same `new_root_id` (or waits for the switch already
   running), so it starts nothing. If the start fails, the offer is marked `failed`
   and the card shows that failure. The same limit never brings the offer back.
8. **Switch back** is offered once, as kind `back` with `back_of` pointing to the
   original switch. brindle offers it when the original provider is in quota again
   and the supervisor it started is still the session's root. If the person
   dismisses it, brindle doesn't offer it again for that switch.
9. **Inbox cards use the existing `InboxItemDTO` fields.** `request_id` holds the
   offer id, `detail` reads "from → to", and `action` is the route above. The app
   card's props (`ProviderSwitchOffer`) are already shaped for a structured DTO.
10. **Only the supervisor switches.** Workers keep their providers, and routing
   already moves new work away from a provider that's out of quota.
11. **Not wired in yet.** `inbox_items` isn't added to `service.inbox.items` and
   `sweep` isn't on the daemon's timer. Both get wired in with the implementation,
   so the stubs never run in production. The route is registered, and
   `api-schema.json` was updated to match.

## Open (needs the owner)

a. Should team policy or routing (`config.py`) limit which providers can be targets?
b. Should autopilot have a setting to switch with no card, for unattended runs?
c. Should brindle also check right away when `quota.record_limit` fires, instead of waiting for the sweep?
d. Add a structured `ProviderSwitchDTO` to `InboxItemDTO` now (this means
   regenerating the schema and `app/src/api/schema.ts`), or parse it from `detail`?
e. Codex and Antigravity transcripts: what does the tail of each format look like?
   The Claude JSONL text blocks are the only format designed so far.
