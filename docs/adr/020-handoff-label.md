# 20. A handoff label is a second way into a Jira track

**Status:** Accepted

**Date:** 2026-11-20

## Context

A Jira track's query is the only way in. It decides which tickets the track
sees, and the worker's eligibility re-check ([004](004-worker-side-claiming.md))
runs the same query again before any write. So a track's queue is also its
gate: a ticket the query excludes cannot be run, even when someone hands it
over on purpose.

Take Acme. It runs a `deps` track that fixes vulnerable dependencies, built
from parts ([018](018-structured-query-inputs.md)):

```toml
[deps]
source = "jira"
project = "SEC"
claim = "label"
claim_label = "bot-claimed"
blocked_transition = "Blocked"

[deps.filters]
Team = ["Payments", "Search"]
```

Its query selects `SEC` tickets that are Open, unassigned, from Payments or
Search, and not labeled `bot-claimed`. Three things Acme wants to do fall
outside it:

1. **Retry.** `SEC-412` failed last week and went to Blocked with
   `bot-claimed` on it. The Search team fixed what blocked it and wants the
   track to try again. Today that means removing the claim label and moving the
   ticket back to Open by hand, and the steps differ for every track.
2. **Try before onboarding.** The Mobile team is not in `filters`. They want
   the track to work one ticket, `SEC-430`, before they decide whether to opt
   in. No label puts that ticket in the query.
3. **Route.** Acme also runs a `router` track. A person assigns any ticket to
   the bot, and `router` decides which track should work it. `router` can only
   hand a ticket to a track whose query happens to select by one label. It
   cannot hand anything to `deps`.

Each case could be solved by editing that track's query, but every track would
need a different edit, and a router would have to know how each target's query
is written.

## Decision

A Jira queue track may set `handoff_label`, one Jira label. A track that does
not set it has no handoff, and nothing below applies to it.

```toml
[deps]
handoff_label = "to-deps"
```

**Query.** Tina builds the track's query as its normal query OR a handoff
branch:

```
(<the track's query>) OR (labels = "<handoff_label>" AND statusCategory != Done [AND project = <project>])
```

The `project` clause is kept only when the track declares `project`, because a
track's skill is written against that project's fields. The handoff branch
ignores `status`, `filters`, `extra`, assignee, and the claim and blocked
labels, so the label alone decides. The same branch is added to a full `query`
override. The ORDER BY stays at the end. The eligibility re-check reads
`track.query`, so it needs no change.

**Claim.** A ticket that carries the handoff label is claimed by consuming the
label. One `PUT` removes it and applies the track's claim (adds `claim_label`,
or assigns the bot), then a re-read confirms the handoff label is gone and the
claim landed. Under `claim = "none"`, removing the label is the whole claim. If
the handoff label is already gone when the worker reads the ticket, another
worker consumed it, and this one exits `no_action_needed`. A claim label
already on the ticket does not block a handoff claim, because the handoff is
newer than the old claim.
Under `claim = "assign"`, the label is consumed and then the usual assignee
compare-and-set runs, so a ticket another user holds still refuses; with the
label gone, it is not offered again.

**Validation.** `handoff_label` must be one label with no whitespace, set only
on a Jira queue track, unique across tracks, and different from that track's
`claim_label` and `blocked_label`.

With this, Acme's three cases are one action each: label `SEC-412` `to-deps`
to retry it, label `SEC-430` `to-deps` to let Mobile try the track, and have
`router` add `to-deps` when it picks `deps`.

## Consequences

- Any track can be handed one ticket by a label, without changes to its query
  and whatever its `filters`, status, or claim history. The mechanism is the
  same on every track, so a router needs only each target's handoff label.
- A track opts in by setting the key. Tina has no naming convention for
  handoff labels.
- A handoff label is a capability: anyone who can edit a ticket's labels can
  start a run of that track on it, including teams the track's `filters` leave
  out. A deployment that wants `filters` to be a hard gate leaves
  `handoff_label` unset.
- A handed-off ticket runs once. The label is consumed at claim, so the next
  dispatch cycle does not offer it again, and a failed run follows the track's
  `on_failure` as usual.
- Tina does not change the ticket's status on handoff. A ticket handed off from
  a status the skill does not expect, such as Blocked, is the skill's to handle.
- When the query becomes a predicate tree (#68), the handoff branch is one `Or`
  node, built once.
