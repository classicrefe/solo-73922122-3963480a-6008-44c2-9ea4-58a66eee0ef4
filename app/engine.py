"""Pure bracket engine: validation, slot resolution, correction simulation.

A bracket is a list of matches (match_id = 1..N in definition order). Each
slot is one of:

* {"type": "team", "team": "X"}          fixed seat
* {"type": "winner", "match_id": k}      winner of match k
* {"type": "loser",  "match_id": k}      loser of match k

Only *active* results propagate. Voided matches produce no winner/loser, so
dependent slots become "tbd" until the match is re-scored.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Optional

from fastapi import HTTPException

MAX_MATCHES = 32


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #

def validate_matches(matches: list[dict]) -> None:
    n = len(matches)
    if not 1 <= n <= MAX_MATCHES:
        raise HTTPException(422, f"bracket must contain 1..{MAX_MATCHES} matches")
    for idx, m in enumerate(matches, start=1):
        for side in ("slot_a", "slot_b"):
            slot = m[side]
            t = slot["type"]
            if t == "team":
                if not slot.get("team") or not str(slot["team"]).strip():
                    raise HTTPException(422, f"match {idx} {side}: team slot needs a team name")
                if slot.get("match_id") is not None:
                    raise HTTPException(422, f"match {idx} {side}: team slot must not reference a match")
            else:
                ref = slot.get("match_id")
                if ref is None:
                    raise HTTPException(422, f"match {idx} {side}: {t} slot needs match_id")
                if not isinstance(ref, int) or not 1 <= ref < idx:
                    raise HTTPException(
                        422,
                        f"match {idx} {side}: feeder match_id must reference an earlier match "
                        f"(got {ref})",
                    )
                if slot.get("team") is not None:
                    raise HTTPException(422, f"match {idx} {side}: {t} slot must not carry a team")
    # feeder references point only at earlier matches (checked above), so the
    # dependency graph is always acyclic; multiple terminal matches (finals,
    # 3rd-place, consolation) are allowed.


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #

@dataclass
class ResolvedSlot:
    state: str          # "team" | "winner" | "loser" | "tbd"
    team: Optional[str]
    source_match_id: Optional[int]


def _slot_spec(match_row, side: str) -> dict:
    return {
        "type": match_row[f"slot_{side}_type"],
        "team": match_row[f"slot_{side}_team"],
        "match_id": match_row[f"slot_{side}_match"],
    }


def resolve_slot(spec: dict, results: dict[int, dict]) -> ResolvedSlot:
    t = spec["type"]
    if t == "team":
        return ResolvedSlot("team", spec["team"], None)
    src = spec["match_id"]
    r = results.get(src)
    if r is None:
        return ResolvedSlot("tbd", None, src)
    team = r["winner"] if t == "winner" else r["loser"]
    return ResolvedSlot(t, team, src)


def resolve_bracket(match_rows, results: dict[int, dict]) -> dict[int, dict]:
    """Return match_id -> {slot_a, slot_b, determined, teams}."""
    out: dict[int, dict] = {}
    for row in match_rows:
        mid = row["match_id"]
        sa = resolve_slot(_slot_spec(row, "a"), results)
        sb = resolve_slot(_slot_spec(row, "b"), results)
        teams = [sa.team, sb.team] if (sa.team and sb.team) else None
        out[mid] = {"slot_a": sa, "slot_b": sb, "determined": teams is not None,
                    "teams": teams}
    return out


def active_result_dicts(rows: dict) -> dict[int, dict]:
    return {mid: dict(r) for mid, r in rows.items()}


# --------------------------------------------------------------------------- #
# Correction simulation
# --------------------------------------------------------------------------- #

def simulate(match_rows, current: dict[int, dict], target_match_id: int,
             new_winner: str) -> dict:
    """Dry-run a correction without touching storage.

    Rules:
      * the target match's two teams must currently be determined and the
        new winner must be one of them;
      * walk matches in definition order with a simulated active-result map;
      * a later match keeps its recorded result iff the same two teams (as a
        set) are still the two resolved teams;
      * if a slot is tbd or either team changed, the result is voided and
        stops propagating, which can cascade into further tbd slots.
    """
    if target_match_id not in {r["match_id"] for r in match_rows}:
        raise HTTPException(404, f"match {target_match_id} does not exist")

    resolved_now = resolve_bracket(match_rows, current)
    target = resolved_now[target_match_id]
    if not target["determined"]:
        raise HTTPException(409, f"match {target_match_id} is not determined yet; cannot correct")
    if new_winner not in target["teams"]:
        raise HTTPException(
            409,
            f"new winner '{new_winner}' is not a participant of match "
            f"{target_match_id}: {target['teams']}",
        )

    old_target = current.get(target_match_id)
    if old_target is None:
        raise HTTPException(409, f"match {target_match_id} has no recorded result to correct")
    if old_target["winner"] == new_winner:
        raise HTTPException(
            409, f"match {target_match_id} already has winner '{new_winner}'; nothing to change"
        )

    teams = target["teams"]
    new_loser = teams[1] if teams[0] == new_winner else teams[0]

    # simulated active results, starting from current state
    sim: dict[int, dict] = dict(current)
    sim[target_match_id] = {
        **old_target,
        "winner": new_winner,
        "loser": new_loser,
    }

    voided: list[int] = []
    retained: list[int] = []

    for row in match_rows:
        mid = row["match_id"]
        if mid <= target_match_id:
            continue  # target replaced; earlier matches untouched
        # resolve the whole bracket each time: feeder slots reference earlier
        # matches that must be present in the input rows
        resolved = resolve_bracket(match_rows, sim)[mid]
        recorded = current.get(mid)
        if recorded is None:
            continue  # never recorded: nothing to void
        if resolved["determined"] and set(resolved["teams"]) == {recorded["winner"],
                                                                 recorded["loser"]}:
            # Same two teams -> the recorded result stays VALID, but the
            # teams may have swapped winner/loser seats. Re-derive winner and
            # loser from the recorded winner's identity so downstream feeder
            # slots see the post-correction ordering.
            teams_now = resolved["teams"]
            winner = recorded["winner"]
            loser = teams_now[1] if teams_now[0] == winner else teams_now[0]
            sim[mid] = {**recorded, "winner": winner, "loser": loser}
            retained.append(mid)
        else:
            voided.append(mid)
            sim.pop(mid, None)   # voided result stops propagating

    projected = resolve_bracket(match_rows, sim)
    return {
        "target_match_id": target_match_id,
        "new_winner": new_winner,
        "new_loser": new_loser,
        "target_teams": teams,
        "voided": voided,
        "retained": retained,
        "sim_results": sim,
        "projected": projected,
    }


# --------------------------------------------------------------------------- #
# Fingerprint: what must stay unchanged between preview and commit
# --------------------------------------------------------------------------- #

def build_fingerprint(match_rows, current: dict[int, dict], target_match_id: int,
                      target_teams: list[str], voided: list[int]) -> str:
    """Hash the facts the re-review rule protects.

    Re-review is required when:
      * the target match's original winner changes, or
      * the to-be-voided list changes (ids or the recorded results behind it),
      * (defensively) the bracket definition or the target's participants
        change.
    """
    payload = {
        "bracket": [
            {
                "match_id": r["match_id"],
                "a": [r["slot_a_type"], r["slot_a_team"], r["slot_a_match"]],
                "b": [r["slot_b_type"], r["slot_b_team"], r["slot_b_match"]],
            }
            for r in match_rows
        ],
        "target_match_id": target_match_id,
        "target_teams": sorted(target_teams),
        "target_winner": current[target_match_id]["winner"],
        "voided": [
            {"match_id": mid,
             "winner": current[mid]["winner"],
             "loser": current[mid]["loser"],
             "version": current[mid]["version"]}
            for mid in voided
        ],
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    return hashlib.sha256(blob).hexdigest()
