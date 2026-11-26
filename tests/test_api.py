"""Key-flow self tests for the reversal API.

Bracket used by the scenario tests:

  m1  A vs B
  m2  C vs D
  m3  W1 vs L1      (same two teams as m1 -> retained on m1 correction)
  m4  W1 vs E       (fixed seat; m1 flip changes the teams -> voided)
  m5  W4 vs W2      (depends on m4 -> tbd cascade -> voided)
  m6  L4 vs L2      (depends on m4 -> tbd cascade -> voided)

Run:  PYTHONPATH=/tmp/pylibs:/workspace python3 tests/test_api.py
"""
from __future__ import annotations

import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="bracket-test-")
os.environ["BRACKET_DB"] = os.path.join(_tmp, "test.db")

from fastapi.testclient import TestClient  # noqa: E402

from app import db  # noqa: E402
from app.main import app  # noqa: E402

client_cm = TestClient(app)
client = client_cm.__enter__()


def team(name: str) -> dict:
    return {"type": "team", "team": name}


def winner_of(mid: int) -> dict:
    return {"type": "winner", "match_id": mid}


def loser_of(mid: int) -> dict:
    return {"type": "loser", "match_id": mid}


BRACKET = [
    {"slot_a": team("A"), "slot_b": team("B")},        # 1
    {"slot_a": team("C"), "slot_b": team("D")},        # 2
    {"slot_a": winner_of(1), "slot_b": loser_of(1)},   # 3
    {"slot_a": winner_of(1), "slot_b": team("E")},     # 4
    {"slot_a": winner_of(4), "slot_b": winner_of(2)},  # 5
    {"slot_a": loser_of(4), "slot_b": loser_of(2)},    # 6
]


def make_bracket() -> int:
    r = client.post("/tournaments", json={"name": "spring cup", "matches": BRACKET})
    assert r.status_code == 201, r.text
    return r.json()["tournament_id"]


def score(tid: int, mid: int, winner: str, sa: int, sb: int):
    r = client.post(f"/tournaments/{tid}/matches/{mid}/score",
                    json={"winner": winner, "score_a": sa, "score_b": sb})
    assert r.status_code == 200, r.text
    return r.json()


def initial_scores(tid: int) -> None:
    # m1 A>B; m2 C>D; m3 (A vs B) B wins; m4 (A vs E) A wins;
    # m5 (A vs C) A wins; m6 (E vs D) E wins
    for mid, win in [(1, "A"), (2, "C"), (3, "B"), (4, "A"), (5, "A"), (6, "E")]:
        score(tid, mid, win, 2, 1)


def preview(tid: int, mid: int, new_winner: str) -> dict:
    r = client.post(f"/tournaments/{tid}/corrections/preview",
                    json={"match_id": mid, "new_winner": new_winner})
    assert r.status_code == 200, r.text
    return r.json()


def test_bracket_validation():
    # 33 matches rejected
    r = client.post("/tournaments", json={
        "name": "too big",
        "matches": [{"slot_a": team("X"), "slot_b": team("Y")}] * 33,
    })
    assert r.status_code == 422
    # forward reference rejected
    r = client.post("/tournaments", json={
        "name": "fwd",
        "matches": [
            {"slot_a": winner_of(2), "slot_b": team("X")},
            {"slot_a": team("Y"), "slot_b": team("Z")},
        ],
    })
    assert r.status_code == 422
    # team slot missing name / feeder slot missing match_id
    r = client.post("/tournaments", json={
        "name": "bad-slot",
        "matches": [{"slot_a": {"type": "team"}, "slot_b": team("Y")}],
    })
    assert r.status_code == 422
    r = client.post("/tournaments", json={
        "name": "bad-slot2",
        "matches": [{"slot_a": {"type": "winner"}, "slot_b": team("Y")}],
    })
    assert r.status_code == 422
    # multiple terminal matches are legal
    r = client.post("/tournaments", json={
        "name": "multi-final",
        "matches": [
            {"slot_a": team("A"), "slot_b": team("B")},
            {"slot_a": team("C"), "slot_b": team("D")},
            {"slot_a": winner_of(1), "slot_b": loser_of(1)},
        ],
    })
    assert r.status_code == 201


def test_scoring_requires_determined_match():
    tid = make_bracket()
    # m4 depends on m1, which has no result yet -> slot tbd
    r = client.post(f"/tournaments/{tid}/matches/4/score",
                    json={"winner": "A", "score_a": 1, "score_b": 0})
    assert r.status_code == 409
    # winner must actually participate
    score(tid, 1, "A", 2, 1)
    r = client.post(f"/tournaments/{tid}/matches/4/score",
                    json={"winner": "C", "score_a": 1, "score_b": 0})
    assert r.status_code == 409


def test_full_flow_retain_void_tbd_cascade_and_commit():
    tid = make_bracket()
    initial_scores(tid)

    pv = preview(tid, 1, "B")
    # m2 untouched and m3 same two teams -> retained; m4 changes teams,
    # m5/m6 lose a feeder (tbd) -> all voided
    assert pv["retained_match_ids"] == [2, 3]
    assert pv["voided_match_ids"] == [4, 5, 6]
    assert [x["match_id"] for x in pv["voided_results"]] == [4, 5, 6]
    assert {x["winner"] for x in pv["voided_results"]} == {"A", "E"}

    proj = {m["match_id"]: m for m in pv["projected_matches"]}
    assert proj[1]["result"]["winner"] == "B"                # corrected
    assert proj[3]["result"]["winner"] == "B"                # retained
    assert proj[4]["result"] is None and proj[4]["determined"] is True
    assert proj[4]["teams"] == ["B", "E"]                    # teams changed
    assert proj[5]["determined"] is False                    # tbd cascade
    assert proj[6]["determined"] is False

    # commit applies correction + voids together
    r = client.post(f"/tournaments/{tid}/corrections/commit",
                    json={"preview_token": pv["preview_token"]})
    assert r.status_code == 200, r.text
    matches = {m["match_id"]: m for m in r.json()["matches"]}
    assert matches[1]["result"]["winner"] == "B"
    assert matches[1]["result"]["version"] == 2
    assert matches[3]["result"]["winner"] == "B"             # retained, active
    assert matches[4]["result"] is None                      # voided
    assert matches[5]["result"] is None
    assert matches[6]["result"] is None
    # voided records remain in history but are inactive
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT match_id, active FROM results WHERE tournament_id=? "
            "ORDER BY match_id, version", (tid,)).fetchall()
    actives = {row["match_id"]: row["active"] for row in rows if row["active"]}
    assert 4 not in actives and 5 not in actives and 6 not in actives
    assert actives[1] == 1 and actives[3] == 1

    # token is single use
    r = client.post(f"/tournaments/{tid}/corrections/commit",
                    json={"preview_token": pv["preview_token"]})
    assert r.status_code == 409


def test_rescoring_after_void_then_repeat_correction():
    tid = make_bracket()
    initial_scores(tid)
    pv = preview(tid, 1, "B")
    r = client.post(f"/tournaments/{tid}/corrections/commit",
                    json={"preview_token": pv["preview_token"]})
    assert r.status_code == 200

    # m4 is now determined with new teams B vs E -> can be re-scored
    score(tid, 4, "E", 0, 2)
    # m5/m6 become determined again after m4 feeds them
    score(tid, 5, "C", 1, 3)
    score(tid, 6, "B", 2, 0)

    # correcting m1 back to A: m4 teams change again {B,E}->{A,E} -> voided
    pv2 = preview(tid, 1, "A")
    assert pv2["voided_match_ids"] == [4, 5, 6]
    r = client.post(f"/tournaments/{tid}/corrections/commit",
                    json={"preview_token": pv2["preview_token"]})
    assert r.status_code == 200
    matches = {m["match_id"]: m for m in r.json()["matches"]}
    assert matches[1]["result"]["winner"] == "A"
    assert matches[1]["result"]["version"] == 3
    assert matches[4]["result"] is None


def test_unrelated_scoring_keeps_preview_valid():
    tid = make_bracket()
    initial_scores(tid)

    # preview a correction on the m2 branch
    pv = preview(tid, 2, "D")
    assert pv["voided_match_ids"] == [5, 6]
    # only later matches appear in the lists; m1 (earlier) is simply untouched
    assert pv["retained_match_ids"] == [3, 4]
    token = pv["preview_token"]

    # unrelated recording on the m1 branch must not invalidate the preview:
    # re-score m1 keeping the same winner (score-only change); target winner
    # (m2) and the voided list (m5,m6) are unaffected
    score(tid, 1, "A", 5, 4)
    r = client.post(f"/tournaments/{tid}/corrections/commit",
                    json={"preview_token": token})
    assert r.status_code == 200, r.text
    matches = {m["match_id"]: m for m in r.json()["matches"]}
    assert matches[1]["result"]["winner"] == "A"   # unchanged winner
    assert matches[1]["result"]["score_a"] == 5    # new score recorded
    assert matches[2]["result"]["winner"] == "D"   # correction applied
    assert matches[3]["result"]["winner"] == "B"   # retained
    assert matches[5]["result"] is None            # voided together
    assert matches[6]["result"] is None


def test_stale_preview_target_winner_change_is_rejected():
    tid = make_bracket()
    initial_scores(tid)
    pv = preview(tid, 1, "B")
    # intervening re-score flips the target's current winner
    score(tid, 1, "B", 0, 2)
    r = client.post(f"/tournaments/{tid}/corrections/commit",
                    json={"preview_token": pv["preview_token"]})
    assert r.status_code == 409


def test_stale_preview_voided_list_change_is_rejected():
    tid = make_bracket()
    initial_scores(tid)
    pv = preview(tid, 1, "B")
    assert pv["voided_match_ids"] == [4, 5, 6]
    # intervening re-score of a voided-list match changes its recorded result
    score(tid, 5, "C", 0, 3)
    r = client.post(f"/tournaments/{tid}/corrections/commit",
                    json={"preview_token": pv["preview_token"]})
    assert r.status_code == 409
    assert "stale" in r.json()["detail"]


def test_re_score_of_voided_match_between_preview_and_commit():
    """Regression: a new result version inserted for a to-be-voided match
    after the preview must still be deactivated at commit time."""
    tid = make_bracket()
    initial_scores(tid)
    pv = preview(tid, 1, "B")
    assert pv["voided_match_ids"] == [4, 5, 6]
    # re-score m5 (still determined under the OLD bracket) with a new version
    score(tid, 5, "C", 0, 3)
    # that change invalidates the old preview -> fresh preview required
    r = client.post(f"/tournaments/{tid}/corrections/commit",
                    json={"preview_token": pv["preview_token"]})
    assert r.status_code == 409
    pv2 = preview(tid, 1, "B")
    r = client.post(f"/tournaments/{tid}/corrections/commit",
                    json={"preview_token": pv2["preview_token"]})
    assert r.status_code == 200
    matches = {m["match_id"]: m for m in r.json()["matches"]}
    assert matches[5]["result"] is None  # new version must be inactive too
    with db.get_conn() as conn:
        versions = conn.execute(
            "SELECT version, active FROM results WHERE tournament_id=? AND match_id=5 "
            "ORDER BY version", (tid,)).fetchall()
    assert [tuple(v) for v in versions] == [(1, 0), (2, 0)]


def test_correction_guards():
    tid = make_bracket()
    # cannot correct a match with no result
    r = client.post(f"/tournaments/{tid}/corrections/preview",
                    json={"match_id": 1, "new_winner": "B"})
    assert r.status_code == 409
    score(tid, 1, "A", 1, 0)
    # new winner must participate
    r = client.post(f"/tournaments/{tid}/corrections/preview",
                    json={"match_id": 1, "new_winner": "C"})
    assert r.status_code == 409
    # no-op correction rejected
    r = client.post(f"/tournaments/{tid}/corrections/preview",
                    json={"match_id": 1, "new_winner": "A"})
    assert r.status_code == 409
    # unknown match / unknown tournament
    r = client.post(f"/tournaments/{tid}/corrections/preview",
                    json={"match_id": 99, "new_winner": "A"})
    assert r.status_code == 404
    r = client.post("/tournaments/999/corrections/preview",
                    json={"match_id": 1, "new_winner": "A"})
    assert r.status_code == 404
    # unknown / cross-tournament token
    r = client.post(f"/tournaments/{tid}/corrections/commit",
                    json={"preview_token": "deadbeef" * 4})
    assert r.status_code == 404


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\nall {len(fns)} tests passed")
