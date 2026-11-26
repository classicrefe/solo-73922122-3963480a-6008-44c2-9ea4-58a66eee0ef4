"""End-to-end API demo: scoring, correction preview, stale-token rejection,
unrelated recording, and atomic commit.

Usage:  python3 scripts/demo.py [base_url]
Requires a running server (uvicorn app.main:app).
"""
from __future__ import annotations

import sys
import time

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"


def team(name: str) -> dict:
    return {"type": "team", "team": name}


def w(mid: int) -> dict:
    return {"type": "winner", "match_id": mid}


def l(mid: int) -> dict:
    return {"type": "loser", "match_id": mid}


def step(title: str) -> None:
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


def main() -> None:
    c = httpx.Client(base_url=BASE, timeout=10)

    step("0. health check")
    r = c.get("/health")
    r.raise_for_status()
    print(r.json())

    step("1. create a 6-match bracket (max 32); seats can be fixed teams, "
         "winners or losers of earlier matches")
    bracket = [
        {"slot_a": team("A"), "slot_b": team("B")},        # 1
        {"slot_a": team("C"), "slot_b": team("D")},        # 2
        {"slot_a": w(1), "slot_b": l(1)},                  # 3 W1 vs L1
        {"slot_a": w(1), "slot_b": team("E")},             # 4 W1 vs E (fixed)
        {"slot_a": w(4), "slot_b": w(2)},                  # 5
        {"slot_a": l(4), "slot_b": l(2)},                  # 6
    ]
    r = c.post("/tournaments", json={"name": "demo cup", "matches": bracket})
    r.raise_for_status()
    tid = r.json()["tournament_id"]
    print(f"tournament_id={tid}")

    step("2. scoring is refused while a seat is tbd (m4 before m1 is played)")
    r = c.post(f"/tournaments/{tid}/matches/4/score",
               json={"winner": "A", "score_a": 1, "score_b": 0})
    print(f"HTTP {r.status_code}: {r.json()['detail']}")

    step("3. record all six results in order")
    planned = [(1, "A"), (2, "C"), (3, "B"), (4, "A"), (5, "A"), (6, "E")]
    for mid, winner in planned:
        r = c.post(f"/tournaments/{tid}/matches/{mid}/score",
                   json={"winner": winner, "score_a": 2, "score_b": 1})
        r.raise_for_status()
        print(f"m{mid} scored, winner={winner}")

    step("4. preview correcting m1: B beats A instead of A beating B")
    r = c.post(f"/tournaments/{tid}/corrections/preview",
               json={"match_id": 1, "new_winner": "B"})
    r.raise_for_status()
    pv = r.json()
    token = pv["preview_token"]
    print(f"preview_token={token}")
    print(f"retained (same two teams): {pv['retained_match_ids']}")
    print(f"to be voided (team changed or seat tbd): {pv['voided_match_ids']}")
    for v in pv["voided_results"]:
        print(f"  void m{v['match_id']}: recorded {v['winner']} beat {v['loser']} "
              f"(v{v['version']})")
    print("projected later matches:")
    for m in pv["projected_matches"]:
        if m["match_id"] <= 1:
            continue
        status = (f"winner={m['result']['winner']}" if m["result"]
                  else "VOIDED -> tbd" if not m["determined"]
                  else f"determined {m['teams']}, awaiting score")
        print(f"  m{m['match_id']} teams={m['teams']} determined={m['determined']} {status}")

    step("5a. an unrelated score entry (same winner, new score) keeps the "
         "preview committable")
    r = c.post(f"/tournaments/{tid}/matches/1/score",
               json={"winner": "A", "score_a": 5, "score_b": 4})
    r.raise_for_status()
    print("m1 re-scored: winner still A, score now 5:4")

    step("5b. but changing a to-be-voided result forces re-review")
    r = c.post(f"/tournaments/{tid}/matches/5/score",
               json={"winner": "C", "score_a": 1, "score_b": 3})
    r.raise_for_status()
    r = c.post(f"/tournaments/{tid}/corrections/commit",
               json={"preview_token": token})
    print(f"HTTP {r.status_code}: {r.json()['detail']}")

    step("6. take a fresh preview and commit: correction and voids apply together")
    r = c.post(f"/tournaments/{tid}/corrections/preview",
               json={"match_id": 1, "new_winner": "B"})
    r.raise_for_status()
    pv2 = r.json()
    print(f"fresh token={pv2['preview_token']}, "
          f"voided={pv2['voided_match_ids']}, retained={pv2['retained_match_ids']}")
    r = c.post(f"/tournaments/{tid}/corrections/commit",
               json={"preview_token": pv2["preview_token"]})
    r.raise_for_status()
    state = r.json()
    for m in state["matches"]:
        if m["result"]:
            res = m["result"]
            print(f"m{m['match_id']} ACTIVE v{res['version']}: {res['winner']} beat "
                  f"{res['loser']} ({res['score_a']}:{res['score_b']})")
        else:
            print(f"m{m['match_id']} no active result | teams={m['teams']} "
                  f"determined={m['determined']}")

    step("7. re-score the voided matches (now fed by the corrected bracket), "
         "then correct m1 back to A")
    for mid, winner in [(4, "E"), (5, "C"), (6, "B")]:
        r = c.post(f"/tournaments/{tid}/matches/{mid}/score",
                   json={"winner": winner, "score_a": 3, "score_b": 2})
        r.raise_for_status()
        print(f"m{mid} re-scored, winner={winner}")
    r = c.post(f"/tournaments/{tid}/corrections/preview",
               json={"match_id": 1, "new_winner": "A"})
    r.raise_for_status()
    pv3 = r.json()
    print(f"second correction preview -> voided={pv3['voided_match_ids']}")
    r = c.post(f"/tournaments/{tid}/corrections/commit",
               json={"preview_token": pv3["preview_token"]})
    r.raise_for_status()
    final = {m["match_id"]: m for m in r.json()["matches"]}
    print(f"m1 winner now: {final[1]['result']['winner']} "
          f"(version {final[1]['result']['version']})")
    print(f"m4 active result: {final[4]['result']}")
    print("\nDEMO OK")


if __name__ == "__main__":
    for _ in range(30):
        try:
            main()
            break
        except httpx.HTTPError as exc:
            print(f"waiting for server... ({exc})")
            time.sleep(1)
    else:
        raise SystemExit("server unreachable")
