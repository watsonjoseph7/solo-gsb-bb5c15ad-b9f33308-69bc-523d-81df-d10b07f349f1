"""Tests for GET /audit/correction-forks: forked correction chains audit."""

import pytest
from fastapi.testclient import TestClient

import app.db as db_module
from app.main import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """TestClient backed by an isolated SQLite file."""
    monkeypatch.setattr(db_module, "DB_PATH", str(tmp_path / "forks-test.db"))
    db_module.init_db()
    return TestClient(app)


def _post(client, event_id, participant_id, points, correction_of=None):
    payload = {
        "event_id": event_id,
        "participant_id": participant_id,
        "points": points,
    }
    if correction_of is not None:
        payload["correction_of"] = correction_of
    r = client.post("/events", json=payload)
    assert r.status_code == 201
    return r.json()


def _forks(client, **params):
    r = client.get("/audit/correction-forks", params=params)
    assert r.status_code == 200, params
    return r.json()


def test_empty_ledger_returns_empty(client):
    body = _forks(client)
    assert body == {"revision": 0, "forks": []}


def test_linear_chain_is_not_returned(client):
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "alice", 12, correction_of="e1")
    _post(client, "e3", "alice", 15, correction_of="e2")
    _post(client, "x1", "bob", 5)  # standalone event, no corrections at all
    body = _forks(client)
    assert body == {"revision": 4, "forks": []}


def test_simple_fork_lists_both_leaves(client):
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "alice", 12, correction_of="e1")
    _post(client, "e3", "alice", 20, correction_of="e1")  # fork off e1
    body = _forks(client)
    assert body["revision"] == 3
    assert body["forks"] == [
        {
            "chain_root": "e1",
            "leaves": [
                {
                    "path": ["e1", "e2"],
                    "participant_id": "alice",
                    "points": 12,
                    "revision": 2,
                },
                {
                    "path": ["e1", "e3"],
                    "participant_id": "alice",
                    "points": 20,
                    "revision": 3,
                },
            ],
            "participant_totals": [
                {"participant_id": "alice", "total_points": 32},
            ],
        }
    ]


def test_chained_corrections_stay_on_their_branch(client):
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "alice", 12, correction_of="e1")
    _post(client, "e3", "bob", 20, correction_of="e1")  # fork off e1
    _post(client, "e4", "alice", 15, correction_of="e2")  # extends e2's branch
    body = _forks(client)
    # e2 is superseded by e4: the branch's effective leaf is e4, reached
    # through the full root-to-leaf path; e3 keeps its own branch.
    assert body["forks"] == [
        {
            "chain_root": "e1",
            "leaves": [
                {
                    "path": ["e1", "e3"],
                    "participant_id": "bob",
                    "points": 20,
                    "revision": 3,
                },
                {
                    "path": ["e1", "e2", "e4"],
                    "participant_id": "alice",
                    "points": 15,
                    "revision": 4,
                },
            ],
            "participant_totals": [
                {"participant_id": "alice", "total_points": 15},
                {"participant_id": "bob", "total_points": 20},
            ],
        }
    ]


def test_snapshot_before_the_fork_hides_the_chain(client):
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "alice", 12, correction_of="e1")
    _post(client, "e3", "alice", 20, correction_of="e1")  # fork appears at rev 3
    assert _forks(client, revision=2) == {"revision": 2, "forks": []}
    assert _forks(client, revision=1) == {"revision": 1, "forks": []}
    assert len(_forks(client, revision=3)["forks"]) == 1


def test_historical_snapshot_restores_correction_relations(client):
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "alice", 12, correction_of="e1")
    _post(client, "e3", "alice", 15, correction_of="e2")  # linear so far
    _post(client, "e4", "bob", 7, correction_of="e1")  # fork off e1 at rev 4
    # at revision 3 the chain is still a plain line: not reported
    assert _forks(client, revision=3) == {"revision": 3, "forks": []}
    body = _forks(client, revision=4)
    (fork,) = body["forks"]
    assert [leaf["path"] for leaf in fork["leaves"]] == [
        ["e1", "e2", "e3"],
        ["e1", "e4"],
    ]


def test_fork_below_the_root_reports_the_chain_root(client):
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "alice", 12, correction_of="e1")  # linear head
    _post(client, "e3", "alice", 13, correction_of="e2")  # fork off e2 ...
    _post(client, "e4", "alice", 14, correction_of="e2")  # ... two direct corrections
    body = _forks(client)
    (fork,) = body["forks"]
    assert fork["chain_root"] == "e1"
    assert [leaf["path"] for leaf in fork["leaves"]] == [
        ["e1", "e2", "e3"],
        ["e1", "e2", "e4"],
    ]


def test_multiple_forks_in_one_chain_are_reported_once(client):
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "alice", 12, correction_of="e1")  # fork 1 at e1 ...
    _post(client, "e3", "alice", 13, correction_of="e1")  # ... (e2, e3)
    _post(client, "e4", "alice", 14, correction_of="e2")  # fork 2 at e2 ...
    _post(client, "e5", "alice", 15, correction_of="e2")  # ... (e4, e5)
    body = _forks(client)
    (fork,) = body["forks"]
    assert fork["chain_root"] == "e1"
    assert [leaf["path"] for leaf in fork["leaves"]] == [
        ["e1", "e3"],
        ["e1", "e2", "e4"],
        ["e1", "e2", "e5"],
    ]
    assert fork["participant_totals"] == [
        {"participant_id": "alice", "total_points": 13 + 14 + 15}
    ]


def test_forks_sorted_by_chain_root(client):
    _post(client, "z1", "zoe", 1)
    _post(client, "z2", "zoe", 2, correction_of="z1")
    _post(client, "z3", "zoe", 3, correction_of="z1")
    _post(client, "a1", "amy", 4)
    _post(client, "a2", "amy", 5, correction_of="a1")
    _post(client, "a3", "amy", 6, correction_of="a1")
    _post(client, "m1", "mike", 7)  # linear: excluded
    body = _forks(client)
    assert [f["chain_root"] for f in body["forks"]] == ["a1", "z1"]


def test_participant_totals_group_and_sort_by_participant(client):
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "carol", 12, correction_of="e1")
    _post(client, "e3", "bob", 20, correction_of="e1")
    _post(client, "e4", "carol", 8, correction_of="e3")  # bob's branch -> carol
    body = _forks(client)
    (fork,) = body["forks"]
    # leaves: e2 (carol, 12) and e4 (carol, 8); bob holds no effective leaf
    assert fork["participant_totals"] == [
        {"participant_id": "carol", "total_points": 20},
    ]
    assert [leaf["participant_id"] for leaf in fork["leaves"]] == [
        "carol",
        "carol",
    ]


def test_revision_zero_returns_empty(client):
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "alice", 12, correction_of="e1")
    _post(client, "e3", "alice", 20, correction_of="e1")
    assert _forks(client, revision=0) == {"revision": 0, "forks": []}


def test_default_revision_is_latest(client):
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "alice", 12, correction_of="e1")
    assert _forks(client) == {"revision": 2, "forks": []}
    _post(client, "e3", "alice", 20, correction_of="e1")
    body = _forks(client)
    assert body["revision"] == 3
    assert len(body["forks"]) == 1


def test_revision_beyond_current_404(client):
    _post(client, "e1", "alice", 10)
    assert (
        client.get("/audit/correction-forks", params={"revision": 2}).status_code
        == 404
    )
    # also on an empty ledger: revision 1 does not exist yet
    r = client.get("/audit/correction-forks", params={"revision": 99})
    assert r.status_code == 404


def test_negative_revision_422(client):
    _post(client, "e1", "alice", 10)
    r = client.get("/audit/correction-forks", params={"revision": -1})
    assert r.status_code == 422


def test_non_integer_revision_422(client):
    r = client.get("/audit/correction-forks", params={"revision": "abc"})
    assert r.status_code == 422


def test_endpoint_is_read_only(client):
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "alice", 12, correction_of="e1")
    _post(client, "e3", "alice", 20, correction_of="e1")
    before = client.get("/events").json()
    _forks(client)
    _forks(client, revision=3)
    assert client.get("/events").json() == before
    # no revision was consumed: the next write gets MAX(revision) + 1
    e4 = _post(client, "e4", "alice", 1)
    assert e4["revision"] == 4


def test_totals_match_leaderboard_per_revision(client):
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "alice", 12, correction_of="e1")
    _post(client, "e3", "bob", 20, correction_of="e1")
    _post(client, "e4", "alice", 15, correction_of="e2")
    for rev in (3, 4):
        body = _forks(client, revision=rev)
        lb = client.get("/leaderboard", params={"revision": rev}).json()
        for fork in body["forks"]:
            for total in fork["participant_totals"]:
                entry = next(
                    e
                    for e in lb["entries"]
                    if e["participant_id"] == total["participant_id"]
                )
                # the chain's per-participant sum matches the participant's
                # effective events that belong to this chain
                chain_points = sum(
                    ev["points"]
                    for ev in entry["effective_events"]
                    if ev["event_id"] in {leaf["path"][-1] for leaf in fork["leaves"]}
                )
                assert total["total_points"] == chain_points
