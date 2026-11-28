from __future__ import annotations

import json
import logging
from typing import Any

import httpx
import pytest

from tina.models import WorkItem
from tina.sources.base import SourceError
from tina.sources.jira import SEARCH_PATH, JiraSource, render_adf

BASE = "https://acme.atlassian.net"
BOT = "bot-account-id"


def issue(
    key: str = "VUL-1",
    assignee: dict[str, Any] | None = None,
    labels: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "key": key,
        "fields": {
            "summary": "CVE-2024-0001 in libfoo",
            "description": {
                "type": "doc",
                "content": [
                    {"type": "paragraph", "content": [{"type": "text", "text": "Bump libfoo."}]}
                ],
            },
            "assignee": assignee,
            "labels": labels or [],
        },
    }


def source(handler, **kwargs: Any) -> JiraSource:
    return JiraSource(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        base_url=BASE,
        bot_account_id=BOT,
        **kwargs,
    )


def test_query_returns_normalized_items() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = request.content
        return httpx.Response(200, json={"issues": [issue(), issue("VUL-2")]})

    items = source(handler).query("project = VUL")

    assert seen["path"] == "/rest/api/3/search/jql"
    assert b"project = VUL" in seen["body"]
    assert [item.id for item in items] == ["VUL-1", "VUL-2"]
    assert items[0].title == "CVE-2024-0001 in libfoo"
    assert items[0].description.strip() == "Bump libfoo."
    assert str(items[0].url) == f"{BASE}/browse/VUL-1"
    assert items[0].raw["key"] == "VUL-1"


def test_get_fetches_one_issue() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/rest/api/3/issue/VUL-1"
        return httpx.Response(200, json=issue())

    assert source(handler).get("VUL-1").id == "VUL-1"


def test_claim_refuses_an_already_assigned_issue(work_item: WorkItem) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(200, json=issue(assignee={"accountId": "someone-else"}))

    assert source(handler).claim(work_item) is False
    assert calls == ["GET"], "an assigned issue is never written to"


def test_claim_assigns_then_confirms(work_item: WorkItem) -> None:
    state: dict[str, Any] = {"assignee": None}
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "PUT":
            state["assignee"] = {"accountId": BOT}
            return httpx.Response(204)
        return httpx.Response(200, json=issue(assignee=state["assignee"]))

    assert source(handler).claim(work_item) is True
    assert [method for method, _ in calls] == ["GET", "PUT", "GET", "GET"]
    assert calls[1][1] == "/rest/api/3/issue/VUL-1/assignee"
    assert calls[3][1] == "/rest/api/3/issue/VUL-1/changelog"


def test_claim_fails_when_the_write_did_not_stick(work_item: WorkItem) -> None:
    """Someone raced us between the PUT and the re-read."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            return httpx.Response(204)
        return httpx.Response(200, json=issue(assignee=None))

    assert source(handler).claim(work_item) is False


def test_claim_prognosis_reports_the_holder_without_writing(work_item: WorkItem) -> None:
    """The verdict comes from a read. A write here would be the bug it exists to avoid."""
    calls: list[str] = []

    def held(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        assert request.method == "GET", "claim_prognosis must never write"
        return httpx.Response(200, json=issue(assignee={"accountId": "someone-else"}))

    def free(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        assert request.method == "GET", "claim_prognosis must never write"
        return httpx.Response(200, json=issue(assignee=None))

    taken = source(held).claim_prognosis(work_item)
    unheld = source(free).claim_prognosis(work_item)

    assert (taken.would_claim, taken.holder) == (False, "someone-else")
    assert (unheld.would_claim, unheld.holder) == (True, "")
    assert calls == ["GET", "GET"], "one read each, and nothing else"


# --- concurrent assignment: the PUT is unconditional, the changelog is not ---

HUMAN = "human-account-id"
CHANGELOG_PATH = "/rest/api/3/issue/VUL-1/changelog"


def assignee_change(change_id: str, author: str, frm: str | None, to: str | None) -> dict[str, Any]:
    return {
        "id": change_id,
        "author": {"accountId": author},
        "items": [{"field": "assignee", "fieldId": "assignee", "from": frm, "to": to}],
    }


class FakeJira:
    """One issue with an assignee and a changelog Jira writes on every change.

    `before_put` runs just ahead of the bot's assignment, which is where a
    concurrent writer lands between the claim's read and its write.
    """

    def __init__(self, before_put: Any = None) -> None:
        self.assignee: str | None = None
        self.history: list[dict[str, Any]] = []
        self.puts: list[str | None] = []
        self.before_put = before_put

    def assign(self, author: str, account: str | None) -> None:
        if account != self.assignee:
            change_id = str(10000 + len(self.history))
            self.history.append(assignee_change(change_id, author, self.assignee, account))
        self.assignee = account

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            assert request.url.path.endswith("/assignee")
            account = json.loads(request.content)["accountId"]
            if self.before_put is not None:
                self.before_put(self)
                self.before_put = None
            self.puts.append(account)
            self.assign(BOT, account)
            return httpx.Response(204)
        if request.url.path == CHANGELOG_PATH:
            start = int(request.url.params.get("startAt", "0"))
            size = int(request.url.params.get("maxResults", "100"))
            page = self.history[start : start + size]
            return httpx.Response(
                200,
                json={
                    "startAt": start,
                    "maxResults": size,
                    "total": len(self.history),
                    "isLast": start + size >= len(self.history),
                    "values": page,
                },
            )
        holder = {"accountId": self.assignee} if self.assignee else None
        return httpx.Response(200, json=issue(assignee=holder))


def test_a_concurrent_human_assignment_survives_the_claim(work_item: WorkItem) -> None:
    """A person assigns the issue between the claim's read and its write.

    Jira's assignee PUT is unconditional, so the bot's write lands on top of
    the person's. The claim must refuse and hand the issue back to them.
    """
    jira = FakeJira(before_put=lambda fake: fake.assign(HUMAN, HUMAN))

    assert source(jira.handler).claim(work_item) is False
    assert jira.assignee == HUMAN, "the person's assignment is restored"
    assert jira.puts == [BOT, HUMAN]


def test_a_restore_never_overwrites_a_later_assignment(work_item: WorkItem) -> None:
    """If someone has already moved the issue on, the restore stands down."""
    jira = FakeJira(before_put=lambda fake: fake.assign(HUMAN, HUMAN))
    real = jira.handler

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == CHANGELOG_PATH:
            jira.assign("someone-else", "someone-else")
        return real(request)

    assert source(handler).claim(work_item) is False
    assert jira.assignee == "someone-else"
    assert jira.puts == [BOT]


def test_a_duplicate_bot_worker_claims_without_a_restore(work_item: WorkItem) -> None:
    """Two workers for the same bot: the second write is a no-op on Jira's
    side and both proceed, the tolerated duplicate-worker race (ADR-004)."""
    jira = FakeJira(before_put=lambda fake: fake.assign(BOT, BOT))

    assert source(jira.handler).claim(work_item) is True
    assert jira.assignee == BOT
    assert jira.puts == [BOT]


def test_an_uncontested_claim_reads_the_changelog_and_stands(work_item: WorkItem) -> None:
    jira = FakeJira()

    assert source(jira.handler).claim(work_item) is True
    assert jira.puts == [BOT]


def test_a_human_handing_the_issue_to_the_bot_is_not_undone(work_item: WorkItem) -> None:
    """Only a change the bot made counts as its own overwrite."""
    jira = FakeJira(before_put=lambda fake: fake.assign(HUMAN, HUMAN))
    real = jira.handler

    def handler(request: httpx.Request) -> httpx.Response:
        response = real(request)
        if request.method == "PUT":
            # The person then reassigns from themselves to the bot on purpose.
            jira.history[-1]["author"] = {"accountId": HUMAN}
        return response

    assert source(handler).claim(work_item) is True
    assert jira.assignee == BOT


def test_the_newest_assignee_change_is_found_past_the_first_page(work_item: WorkItem) -> None:
    jira = FakeJira(before_put=lambda fake: fake.assign(HUMAN, HUMAN))
    for n in range(150):
        jira.history.append({"id": str(n), "author": {"accountId": HUMAN}, "items": []})

    assert source(jira.handler).claim(work_item) is False
    assert jira.assignee == HUMAN


def test_an_unreadable_changelog_stops_the_claim(work_item: WorkItem) -> None:
    """Without the evidence the claim cannot be confirmed; the harness must not start."""
    jira = FakeJira()
    real = jira.handler

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == CHANGELOG_PATH:
            return httpx.Response(403, text="forbidden")
        return real(request)

    with pytest.raises(SourceError, match="changelog"):
        source(handler).claim(work_item)


# --- claim policies: assign is idempotent, label is a set add (ADR-014) ------


def test_a_bot_held_issue_reclaims_successfully(work_item: WorkItem) -> None:
    """The reclaim deadlock: an item reopened while the bot holds it must not
    exit no_action_needed forever."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(200, json=issue(assignee={"accountId": BOT}))

    assert source(handler).claim(work_item) is True
    assert calls == ["GET"], "the bot already holds it; nothing is written"


def test_claim_prognosis_reports_a_bot_held_issue_as_claimable(work_item: WorkItem) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET", "claim_prognosis must never write"
        return httpx.Response(200, json=issue(assignee={"accountId": BOT}))

    prognosis = source(handler).claim_prognosis(work_item)

    assert (prognosis.would_claim, prognosis.holder) == (True, BOT)


def test_claim_transition_is_applied_after_a_successful_assign(work_item: WorkItem) -> None:
    state: dict[str, Any] = {"assignee": None}
    calls: list[tuple[str, str]] = []
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.path == "/rest/api/3/issue/VUL-1/transitions":
            if request.method == "GET":
                return httpx.Response(
                    200,
                    json={"transitions": [{"id": "21", "name": "In Progress"}]},
                )
            seen["body"] = json.loads(request.content)
            return httpx.Response(204)
        if request.method == "PUT":
            state["assignee"] = {"accountId": BOT}
            return httpx.Response(204)
        return httpx.Response(200, json=issue(assignee=state["assignee"]))

    jira = source(handler, claim_transition="In Progress")

    assert jira.claim(work_item) is True
    assert calls[-2:] == [
        ("GET", "/rest/api/3/issue/VUL-1/transitions"),
        ("POST", "/rest/api/3/issue/VUL-1/transitions"),
    ], "the transition comes after the claim is confirmed"
    assert seen["body"] == {"transition": {"id": "21"}}


def test_an_unavailable_transition_is_logged_and_the_claim_stands(
    work_item: WorkItem, caplog: pytest.LogCaptureFixture
) -> None:
    """The item is assigned and the work proceeds; a stale status is the lesser bug."""
    state: dict[str, Any] = {"assignee": None}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/transitions"):
            return httpx.Response(200, json={"transitions": [{"id": "31", "name": "Done"}]})
        if request.method == "PUT":
            state["assignee"] = {"accountId": BOT}
            return httpx.Response(204)
        return httpx.Response(200, json=issue(assignee=state["assignee"]))

    with caplog.at_level(logging.WARNING):
        claimed = source(handler, claim_transition="In Progress").claim(work_item)

    assert claimed is True
    assert any("transition" in record.message for record in caplog.records)


def test_a_failed_transition_is_logged_and_the_claim_stands(
    work_item: WorkItem, caplog: pytest.LogCaptureFixture
) -> None:
    state: dict[str, Any] = {"assignee": None}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/transitions"):
            return httpx.Response(500, text="boom")
        if request.method == "PUT":
            state["assignee"] = {"accountId": BOT}
            return httpx.Response(204)
        return httpx.Response(200, json=issue(assignee=state["assignee"]))

    with caplog.at_level(logging.WARNING):
        claimed = source(handler, claim_transition="In Progress").claim(work_item)

    assert claimed is True
    assert any("transition" in record.message for record in caplog.records)


def label_source(handler) -> JiraSource:
    return source(handler, claim_policy="label", claim_label="bot-claimed")


def test_a_label_claim_adds_the_label_then_confirms(work_item: WorkItem) -> None:
    state: dict[str, list[str]] = {"labels": []}
    calls: list[tuple[str, str]] = []
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "PUT":
            seen["body"] = json.loads(request.content)
            state["labels"] = ["bot-claimed"]
            return httpx.Response(204)
        return httpx.Response(200, json=issue(labels=state["labels"]))

    assert label_source(handler).claim(work_item) is True
    assert [method for method, _ in calls] == ["GET", "PUT", "GET"]
    assert seen["body"] == {"update": {"labels": [{"add": "bot-claimed"}]}}


def test_a_label_claim_refuses_an_already_labeled_issue(work_item: WorkItem) -> None:
    """The label carries no identity, so present always means someone else holds it."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(200, json=issue(labels=["bot-claimed"]))

    assert label_source(handler).claim(work_item) is False
    assert calls == ["GET"], "a labeled issue is never written to"


def test_a_label_claim_fails_when_the_write_did_not_stick(work_item: WorkItem) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            return httpx.Response(204)
        return httpx.Response(200, json=issue(labels=[]))

    assert label_source(handler).claim(work_item) is False


def test_claim_prognosis_under_a_label_claim(work_item: WorkItem) -> None:
    def held(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET", "claim_prognosis must never write"
        return httpx.Response(200, json=issue(labels=["bot-claimed", "bug"]))

    def free(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET", "claim_prognosis must never write"
        return httpx.Response(200, json=issue(labels=["bug"]))

    taken = label_source(held).claim_prognosis(work_item)
    unheld = label_source(free).claim_prognosis(work_item)

    assert (taken.would_claim, taken.holder) == (False, "label:bot-claimed")
    assert (unheld.would_claim, unheld.holder) == (True, "")


def test_claimed_under_a_label_claim_inverts_the_negated_label_clause() -> None:
    """The documented exclusion shapes, with the rest of the query preserved."""
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content)["jql"])
        return httpx.Response(200, json={"issues": [issue()]})

    cases = [
        # the shape the config example documents: labels IS EMPTY also matches
        (
            'project = VUL AND (labels IS EMPTY OR labels != "bot-claimed")',
            'project = VUL AND labels = "bot-claimed"',
        ),
        # the compound in the other order
        (
            '(labels != "bot-claimed" OR labels IS EMPTY) AND project = VUL',
            'labels = "bot-claimed" AND project = VUL',
        ),
        ('project = VUL AND labels != "bot-claimed"', 'project = VUL AND labels = "bot-claimed"'),
        ("labels != bot-claimed", 'labels = "bot-claimed"'),
    ]
    for jql, _ in cases:
        label_source(handler).claimed(jql)

    assert sent == [expected for _, expected in cases]


def test_a_query_with_no_negated_claim_label_is_a_source_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("the query is rejected before any request is made")

    for jql in ("project = VUL", 'project = VUL AND labels != "other-label"'):
        with pytest.raises(SourceError) as caught:
            label_source(handler).claimed(jql)
        assert jql in str(caught.value)
        assert "bot-claimed" in caught.value.fix


def test_claimed_under_claim_none_is_empty_without_a_search() -> None:
    """The bot never holds anything, and asking the tracker would imply it could."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("claim = 'none' has no claims to count")

    assert source(handler, claim_policy="none").claimed("project = VUL") == []


# --- handoff: a label consumed at claim (ADR-020) -----------------------------


def handoff_source(handler, policy: str) -> JiraSource:
    label = "bot-claimed" if policy == "label" else None
    return source(handler, claim_policy=policy, claim_label=label, handoff_label="to-deps")


@pytest.mark.parametrize(
    ("policy", "before", "edits", "after"),
    [
        (
            "label",
            ["to-deps", "bot-claimed"],
            [{"remove": "to-deps"}, {"add": "bot-claimed"}],
            ["bot-claimed"],
        ),
        ("label", ["to-deps"], [{"remove": "to-deps"}, {"add": "bot-claimed"}], ["bot-claimed"]),
        ("none", ["to-deps", "bug"], [{"remove": "to-deps"}], ["bug"]),
    ],
)
def test_a_handoff_claim_consumes_the_label_in_one_write(
    work_item: WorkItem,
    policy: str,
    before: list[str],
    edits: list[dict[str, str]],
    after: list[str],
) -> None:
    """A stale claim label does not refuse a handoff: the handoff is newer."""
    state = {"labels": before}
    calls: list[str] = []
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        if request.method == "PUT":
            seen["body"] = json.loads(request.content)
            state["labels"] = after
            return httpx.Response(204)
        return httpx.Response(200, json=issue(labels=state["labels"]))

    assert handoff_source(handler, policy).claim(work_item) is True
    assert calls == ["GET", "PUT", "GET"]
    assert seen["body"] == {"update": {"labels": edits}}


@pytest.mark.parametrize("policy", ["label", "none", "assign"])
def test_a_handoff_claim_fails_when_the_label_is_still_there(
    work_item: WorkItem, policy: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            return httpx.Response(204)
        return httpx.Response(200, json=issue(labels=["to-deps"]))

    assert handoff_source(handler, policy).claim(work_item) is False


@pytest.mark.parametrize(
    ("holder", "expected", "writes"),
    [
        (None, True, ["/rest/api/3/issue/VUL-1", "/rest/api/3/issue/VUL-1/assignee"]),
        ("someone-else", False, ["/rest/api/3/issue/VUL-1"]),
    ],
)
def test_a_handoff_under_assign_consumes_the_label_then_assigns(
    work_item: WorkItem, holder: str | None, expected: bool, writes: list[str]
) -> None:
    """A ticket a person holds still refuses, but the label is gone, so it is
    not offered again."""
    state: dict[str, Any] = {
        "labels": ["to-deps"],
        "assignee": {"accountId": holder} if holder else None,
    }
    puts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            puts.append(request.url.path)
            if request.url.path.endswith("/assignee"):
                state["assignee"] = {"accountId": BOT}
            else:
                state["labels"] = []
            return httpx.Response(204)
        return httpx.Response(200, json=issue(assignee=state["assignee"], labels=state["labels"]))

    assert handoff_source(handler, "assign").claim(work_item) is expected
    assert puts == writes


@pytest.mark.parametrize(
    ("policy", "labels", "would_claim", "holder"),
    [
        ("label", ["to-deps", "bot-claimed"], True, ""),
        ("label", ["bot-claimed"], False, "label:bot-claimed"),
        ("none", [], True, ""),
    ],
)
def test_claim_prognosis_with_a_handoff_label(
    work_item: WorkItem, policy: str, labels: list[str], would_claim: bool, holder: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET", "claim_prognosis must never write"
        return httpx.Response(200, json=issue(labels=labels))

    prognosis = handoff_source(handler, policy).claim_prognosis(work_item)

    assert (prognosis.would_claim, prognosis.holder) == (would_claim, holder)


def test_claimed_leaves_out_tickets_still_carrying_the_handoff_label() -> None:
    """A ticket with the handoff label is queued, not held."""
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content)["jql"])
        return httpx.Response(200, json={"issues": []})

    jql = (
        '(project = SEC AND (labels IS EMPTY OR labels != "bot-claimed"))'
        ' OR (project = SEC AND labels = "to-deps" AND statusCategory != Done)'
        " ORDER BY created ASC"
    )
    handoff_source(handler, "label").claimed(jql)

    assert sent == [
        '((project = SEC AND labels = "bot-claimed")'
        ' OR (project = SEC AND labels = "to-deps" AND statusCategory != Done))'
        ' AND (labels IS EMPTY OR labels != "to-deps") ORDER BY created ASC'
    ]


# --- matches: the configured query, scoped to one item -----------------------


def test_matches_scopes_the_query_to_the_item() -> None:
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content)["jql"])
        return httpx.Response(200, json={"issues": [issue()]})

    eligible = source(handler).matches("VUL-1", "project = VUL AND assignee IS EMPTY")

    assert eligible is True
    assert sent == ['(project = VUL AND assignee IS EMPTY) AND key = "VUL-1"']


def test_matches_strips_the_order_by_clause() -> None:
    """`ORDER BY` cannot sit inside the parenthesized predicate."""
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content)["jql"])
        return httpx.Response(200, json={"issues": [issue()]})

    source(handler).matches("VUL-1", "project = VUL ORDER BY created DESC")

    assert sent == ['(project = VUL) AND key = "VUL-1"']


def test_a_gone_stale_item_no_longer_matches() -> None:
    """The whole predicate is re-checked, so any exclusion mechanism works."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"issues": []})

    assert source(handler).matches("VUL-1", "project = VUL") is False


def test_matches_searches_and_never_writes() -> None:
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        return httpx.Response(200, json={"issues": [issue()]})

    source(handler).matches("VUL-1", "project = VUL")

    assert calls == [("POST", SEARCH_PATH)], "one search, and nothing else"


def test_http_error_is_a_source_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="forbidden")

    with pytest.raises(SourceError, match="403"):
        source(handler).query("project = VUL")


def test_missing_env_is_named() -> None:
    with pytest.raises(SourceError, match="JIRA_BASE_URL"):
        JiraSource(client=httpx.Client())


# --- transient retry: server errors and rate limits ---------------------------


def test_server_errors_are_retried_on_a_short_ladder() -> None:
    responses = iter([httpx.Response(500), httpx.Response(503)])

    def handler(request: httpx.Request) -> httpx.Response:
        return next(responses, httpx.Response(200, json={"issues": [issue()]}))

    waits: list[float] = []
    items = source(handler, sleep=waits.append).query("project = VUL")

    assert [i.id for i in items] == ["VUL-1"]
    assert waits == [2.0, 8.0]


def test_a_persistent_server_error_raises_after_the_ladder() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="bad gateway")

    waits: list[float] = []
    with pytest.raises(SourceError, match="502: bad gateway"):
        source(handler, sleep=waits.append).query("project = VUL")

    assert waits == [2.0, 8.0]


def test_a_rate_limit_waits_what_retry_after_asks() -> None:
    responses = iter([httpx.Response(429, headers={"Retry-After": "7"})])

    def handler(request: httpx.Request) -> httpx.Response:
        return next(responses, httpx.Response(200, json={"issues": [issue()]}))

    waits: list[float] = []
    items = source(handler, sleep=waits.append).query("project = VUL")

    assert [i.id for i in items] == ["VUL-1"]
    assert waits == [7.0]


def test_retry_after_is_capped_so_the_server_cannot_demand_an_hour() -> None:
    responses = iter([httpx.Response(429, headers={"Retry-After": "3600"})])

    def handler(request: httpx.Request) -> httpx.Response:
        return next(responses, httpx.Response(200, json={"issues": [issue()]}))

    waits: list[float] = []
    source(handler, sleep=waits.append).query("project = VUL")

    assert waits == [60.0]


def test_a_4xx_other_than_the_rate_limit_is_never_retried() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="forbidden")

    waits: list[float] = []
    with pytest.raises(SourceError, match="403"):
        source(handler, sleep=waits.append).query("project = VUL")

    assert waits == []


@pytest.mark.parametrize(
    ("document", "expected"),
    [
        (None, ""),
        ("plain string", "plain string"),
        ({"type": "text", "text": "hi"}, "hi"),
        (
            {
                "type": "doc",
                "content": [
                    {"type": "paragraph", "content": [{"type": "text", "text": "one"}]},
                    {"type": "paragraph", "content": [{"type": "text", "text": "two"}]},
                ],
            },
            "one\ntwo\n",
        ),
    ],
)
def test_render_adf(document: Any, expected: str) -> None:
    assert render_adf(document) == expected


def test_a_response_of_the_wrong_shape_is_a_source_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"issues": "not a list"})

    with pytest.raises(SourceError, match="unexpected response"):
        source(handler).query("project = VUL")


def test_non_json_is_a_source_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>maintenance</html>")

    with pytest.raises(SourceError, match="unexpected response"):
        source(handler).get("VUL-1")


def test_unknown_fields_are_tolerated() -> None:
    """Trackers grow fields; that is not a reason to stop working."""

    def handler(request: httpx.Request) -> httpx.Response:
        payload = issue()
        payload["fields"]["someNewField"] = {"anything": True}
        payload["expand"] = "renderedFields"
        return httpx.Response(200, json=payload)

    item = source(handler).get("VUL-1")

    assert item.id == "VUL-1"
    assert item.raw["fields"]["someNewField"] == {"anything": True}, "raw keeps everything"


def test_claimed_swaps_the_empty_assignee_clause() -> None:
    """Every spelling of emptiness, with the rest of the query preserved."""
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content)["jql"])
        return httpx.Response(200, json={"issues": [issue(), issue("VUL-2")]})

    cases = [
        # the architecture doc's example: the other clauses survive untouched
        (
            "project = VUL AND status = Open AND assignee IS EMPTY",
            f'project = VUL AND status = Open AND assignee = "{BOT}"',
        ),
        # mid-query, and lowercase
        (
            "project = VUL AND assignee is empty AND status = Open",
            f'project = VUL AND assignee = "{BOT}" AND status = Open',
        ),
        ("assignee IS EMPTY", f'assignee = "{BOT}"'),
        ("assignee IS NULL", f'assignee = "{BOT}"'),
        ("assignee = EMPTY", f'assignee = "{BOT}"'),
        ("assignee=NULL", f'assignee = "{BOT}"'),
        ("ASSIGNEE   IS    EMPTY", f'assignee = "{BOT}"'),
    ]
    items_returned = [source(handler).claimed(jql) for jql, _ in cases]

    assert sent == [expected for _, expected in cases]
    assert [item.id for item in items_returned[0]] == ["VUL-1", "VUL-2"]
    assert items_returned[0][0].title == "CVE-2024-0001 in libfoo"
    assert str(items_returned[0][0].url) == f"{BASE}/browse/VUL-1"


def test_claimed_issues_no_write() -> None:
    """One search, and nothing else. A write here would break the read-only contract."""
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        assert request.method == "POST", "claimed() must never write"
        assert request.url.path == SEARCH_PATH, "the only POST it makes is the search"
        return httpx.Response(200, json={"issues": [issue()]})

    source(handler).claimed("project = VUL AND assignee IS EMPTY")

    assert calls == [("POST", SEARCH_PATH)]


def test_a_query_with_no_empty_assignee_clause_is_a_source_error() -> None:
    """`IS NOT EMPTY` means the opposite, so it is not a match — loud beats a silent 0."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("the query is rejected before any request is made")

    for jql in (
        "project = VUL",
        "project = VUL AND assignee IS NOT EMPTY",
        "project = VUL AND assignee != EMPTY",
    ):
        with pytest.raises(SourceError) as caught:
            source(handler).claimed(jql)
        assert jql in str(caught.value)
        assert "assignee IS EMPTY" in caught.value.fix


# --- lifecycle write-back: annotate and block (ADR-013) ----------------------


def test_annotate_posts_the_comment_as_adf(work_item: WorkItem) -> None:
    """One paragraph per line, in the same document shape `render_adf` reads."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json={})

    source(handler).annotate(work_item, "run ended failed\n\nRun logs: https://logs.example/1")

    assert (seen["method"], seen["path"]) == ("POST", "/rest/api/3/issue/VUL-1/comment")
    document = seen["body"]["body"]
    assert document["type"] == "doc"
    texts = [node["content"][0]["text"] for node in document["content"] if node.get("content")]
    assert texts == ["run ended failed", "Run logs: https://logs.example/1"]


def test_annotate_failure_is_logged_and_swallowed(
    work_item: WorkItem, caplog: pytest.LogCaptureFixture
) -> None:
    """A reporting hiccup must not mask the failure it reports."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    with caplog.at_level(logging.WARNING):
        source(handler).annotate(work_item, "run ended failed")

    assert any("annotate failed" in record.message for record in caplog.records)


def test_block_adds_the_exclusion_label(work_item: WorkItem) -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(204)

    source(handler).block(work_item)

    assert (seen["method"], seen["path"]) == ("PUT", "/rest/api/3/issue/VUL-1")
    assert seen["body"] == {"update": {"labels": [{"add": "tina-blocked"}]}}


def test_the_exclusion_label_is_overridable(work_item: WorkItem) -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(204)

    source(handler, blocked_label="factory-hold").block(work_item)

    assert seen["body"] == {"update": {"labels": [{"add": "factory-hold"}]}}


def test_block_is_idempotent(work_item: WorkItem) -> None:
    """Jira's label add is a set add: blocking an already-blocked issue is a no-op."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(204)

    jira = source(handler)
    jira.block(work_item)
    jira.block(work_item)

    assert calls == ["PUT", "PUT"], "the same write twice, and no error either time"


def test_block_failure_is_logged_and_swallowed(
    work_item: WorkItem, caplog: pytest.LogCaptureFixture
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="no permission")

    with caplog.at_level(logging.WARNING):
        source(handler).block(work_item)

    assert any("block failed" in record.message for record in caplog.records)


# --- bot identity, login, block by transition ----------------------------------


def test_the_bot_account_is_looked_up_from_myself_when_unset() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json={"accountId": "me-123", "displayName": "Bot"})

    src = JiraSource(client=httpx.Client(transport=httpx.MockTransport(handler)), base_url=BASE)

    assert src.bot_account_id == "me-123"
    assert src.bot_account_id == "me-123"
    assert calls == ["/rest/api/3/myself"], "looked up once, then cached"


def test_the_env_bot_account_id_is_still_honored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JIRA_BOT_ACCOUNT_ID", "from-env")

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no lookup when configured")

    src = JiraSource(client=httpx.Client(transport=httpx.MockTransport(handler)), base_url=BASE)

    assert src.bot_account_id == "from-env"


def test_login_reports_who_the_credentials_act_as() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"accountId": "me-123"})

    assert (
        JiraSource(
            client=httpx.Client(transport=httpx.MockTransport(handler)), base_url=BASE
        ).login()
        == "me-123"
    )


def test_login_raises_when_the_tracker_refuses() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="nope")

    with pytest.raises(SourceError, match="401"):
        JiraSource(
            client=httpx.Client(transport=httpx.MockTransport(handler)), base_url=BASE
        ).login()


def test_block_transitions_when_a_blocked_transition_is_configured(work_item: WorkItem) -> None:
    calls: list[tuple[str, str, bytes]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, request.content))
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "transitions": [
                        {"id": "11", "name": "Start work", "to": {"name": "In Progress"}},
                        {"id": "31", "name": "Block it", "to": {"name": "Blocked"}},
                    ]
                },
            )
        return httpx.Response(204)

    source(handler, blocked_transition="Blocked").block(work_item)

    assert [c[:2] for c in calls] == [
        ("GET", "/rest/api/3/issue/VUL-1/transitions"),
        ("POST", "/rest/api/3/issue/VUL-1/transitions"),
    ]
    assert json.loads(calls[1][2]) == {"transition": {"id": "31"}}, "matched on the target status"


def test_block_by_transition_matches_a_transition_name_too(work_item: WorkItem) -> None:
    posted: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"transitions": [{"id": "5", "name": "Blocked"}]})
        posted.append(request.content)
        return httpx.Response(204)

    source(handler, blocked_transition="blocked").block(work_item)

    assert json.loads(posted[0]) == {"transition": {"id": "5"}}


def test_block_by_transition_is_best_effort(
    work_item: WorkItem, caplog: pytest.LogCaptureFixture
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"transitions": []})

    with caplog.at_level(logging.WARNING):
        source(handler, blocked_transition="Blocked").block(work_item)

    assert "block transition not available" in caplog.text
