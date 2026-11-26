"""FastAPI application: bracket definition, scoring, correction preview/commit."""
from __future__ import annotations

import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

from . import db
from .engine import (MAX_MATCHES, active_result_dicts, build_fingerprint,
                     resolve_bracket, simulate, validate_matches)
from .models import (CommitIn, CorrectionPreviewIn, MatchView, PreviewView,
                     ResultView, ScoreIn, SlotView, TournamentIn,
                     TournamentView, VoidedResultView)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="Tournament Reversal API", version="1.0.0", lifespan=lifespan)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


# --------------------------------------------------------------------------- #
# View helpers
# --------------------------------------------------------------------------- #

def _slot_view(slot) -> SlotView:
    return SlotView(state=slot.state, team=slot.team, source_match_id=slot.source_match_id)


def _result_view(row) -> ResultView:
    return ResultView(
        active=bool(row["active"]), winner=row["winner"], loser=row["loser"],
        score_a=row["score_a"], score_b=row["score_b"], note=row["note"],
        version=row["version"],
    )


def build_match_views(match_rows, results: dict[int, dict]) -> list[MatchView]:
    resolved = resolve_bracket(match_rows, results)
    views: list[MatchView] = []
    for row in match_rows:
        mid = row["match_id"]
        info = resolved[mid]
        result_row = results.get(mid)
        views.append(MatchView(
            match_id=mid,
            slot_a=_slot_view(info["slot_a"]),
            slot_b=_slot_view(info["slot_b"]),
            determined=info["determined"],
            teams=info["teams"],
            result=_result_view(result_row) if result_row else None,
        ))
    return views


def _load_tournament(conn, tournament_id: int):
    t = conn.execute("SELECT * FROM tournaments WHERE id=?", (tournament_id,)).fetchone()
    if t is None:
        raise HTTPException(404, f"tournament {tournament_id} not found")
    match_rows = db.load_matches(conn, tournament_id)
    if not match_rows:
        raise HTTPException(500, "tournament has no matches")  # pragma: no cover
    return t, match_rows


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #

@app.post("/tournaments", response_model=TournamentView, status_code=201)
def create_tournament(payload: TournamentIn):
    if len(payload.matches) > MAX_MATCHES:
        raise HTTPException(422, f"at most {MAX_MATCHES} matches per bracket")
    matches = [m.model_dump() for m in payload.matches]
    validate_matches(matches)

    with db.transaction() as conn:
        cur = conn.execute(
            "INSERT INTO tournaments(name, version, created_at) VALUES (?,1,?)",
            (payload.name, db.now_iso()),
        )
        tid = cur.lastrowid
        for idx, m in enumerate(matches, start=1):
            conn.execute(
                "INSERT INTO matches(tournament_id, match_id, slot_a_type, slot_a_team, "
                "slot_a_match, slot_b_type, slot_b_team, slot_b_match) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (tid, idx,
                 m["slot_a"]["type"], m["slot_a"]["team"], m["slot_a"]["match_id"],
                 m["slot_b"]["type"], m["slot_b"]["team"], m["slot_b"]["match_id"]),
            )
        match_rows = db.load_matches(conn, tid)
        results = active_result_dicts(db.active_results(conn, tid))
        views = build_match_views(match_rows, results)
    return TournamentView(tournament_id=tid, name=payload.name, matches=views)


@app.get("/tournaments/{tournament_id}", response_model=TournamentView)
def get_tournament(tournament_id: int):
    with db.get_conn() as conn:
        t, match_rows = _load_tournament(conn, tournament_id)
        results = active_result_dicts(db.active_results(conn, tournament_id))
        views = build_match_views(match_rows, results)
    return TournamentView(tournament_id=t["id"], name=t["name"], matches=views)


@app.post("/tournaments/{tournament_id}/matches/{match_id}/score",
          response_model=MatchView)
def record_score(tournament_id: int, match_id: int, payload: ScoreIn):
    with db.transaction() as conn:
        _, match_rows = _load_tournament(conn, tournament_id)
        if match_id not in {r["match_id"] for r in match_rows}:
            raise HTTPException(404, f"match {match_id} does not exist")
        results = active_result_dicts(db.active_results(conn, tournament_id))
        resolved = resolve_bracket(match_rows, results)[match_id]
        if not resolved["determined"]:
            raise HTTPException(
                409,
                f"match {match_id} is not determined yet "
                f"(slot states: {resolved['slot_a'].state}, "
                f"{resolved['slot_b'].state}); "
                "scoring is only allowed after both seats are set",
            )
        if payload.winner not in resolved["teams"]:
            raise HTTPException(
                409,
                f"winner '{payload.winner}' is not a participant of match "
                f"{match_id}: {resolved['teams']}",
            )
        loser = resolved["teams"][1] if resolved["teams"][0] == payload.winner \
            else resolved["teams"][0]
        # deactivate previous active version(s), then insert the new one
        conn.execute(
            "UPDATE results SET active=0 WHERE tournament_id=? AND match_id=? "
            "AND active=1",
            (tournament_id, match_id),
        )
        version = db.max_result_version(conn, tournament_id, match_id) + 1
        conn.execute(
            "INSERT INTO results(tournament_id, match_id, version, active, winner, "
            "loser, score_a, score_b, note) VALUES (?,?,?,1,?,?,?,?,?)",
            (tournament_id, match_id, version, payload.winner, loser,
             payload.score_a, payload.score_b, payload.note),
        )
        results = active_result_dicts(db.active_results(conn, tournament_id))
        views = {v.match_id: v for v in build_match_views(match_rows, results)}
    return views[match_id]


@app.post("/tournaments/{tournament_id}/corrections/preview",
          response_model=PreviewView)
def correction_preview(tournament_id: int, payload: CorrectionPreviewIn):
    with db.transaction() as conn:
        _, match_rows = _load_tournament(conn, tournament_id)
        current = active_result_dicts(db.active_results(conn, tournament_id))
        sim = simulate(match_rows, current, payload.match_id, payload.new_winner)

        fingerprint = build_fingerprint(
            match_rows, current, payload.match_id,
            sim["target_teams"], sim["voided"],
        )
        token = secrets.token_hex(16)
        voided_payload = [
            {"match_id": mid, **current[mid]} for mid in sim["voided"]
        ]
        db.insert_preview(
            conn, token, tournament_id, payload.match_id, payload.new_winner,
            fingerprint, voided_payload,
        )

        projected_views = build_match_views(match_rows, sim["sim_results"])
        voided_views = [
            VoidedResultView(match_id=mid, **_result_view(current[mid]).model_dump())
            for mid in sim["voided"]
        ]
    return PreviewView(
        preview_token=token,
        target_match_id=payload.match_id,
        new_winner=payload.new_winner,
        corrected_match={
            "match_id": payload.match_id,
            "new_teams": sim["target_teams"],
            "new_winner": payload.new_winner,
        },
        voided_match_ids=sim["voided"],
        voided_results=voided_views,
        retained_match_ids=sim["retained"],
        projected_matches=projected_views,
    )


@app.post("/tournaments/{tournament_id}/corrections/commit",
          response_model=TournamentView)
def correction_commit(tournament_id: int, payload: CommitIn):
    with db.transaction() as conn:
        preview = conn.execute(
            "SELECT * FROM previews WHERE token=?", (payload.preview_token,)
        ).fetchone()
        if preview is None:
            raise HTTPException(404, "unknown preview token")
        if preview["consumed"]:
            raise HTTPException(409, "preview token already used; request a new preview")
        if preview["tournament_id"] != tournament_id:
            raise HTTPException(409, "preview belongs to a different tournament")

        _, match_rows = _load_tournament(conn, tournament_id)
        current = active_result_dicts(db.active_results(conn, tournament_id))
        target_match_id = preview["target_match_id"]
        new_winner = preview["new_winner"]

        # Re-run the simulation against the CURRENT state.
        try:
            sim = simulate(match_rows, current, target_match_id, new_winner)
        except HTTPException as exc:
            raise HTTPException(409, f"state changed since preview: {exc.detail}")

        fresh_fingerprint = build_fingerprint(
            match_rows, current, target_match_id,
            sim["target_teams"], sim["voided"],
        )
        if fresh_fingerprint != preview["fingerprint"]:
            raise HTTPException(
                409,
                "preview is stale: the target's original winner or the "
                "to-be-voided result list changed; review and preview again",
            )

        # All checks pass: apply target replacement + voids atomically.
        target_row = current[target_match_id]
        # deactivate every currently-active row of the target match: the
        # corrected row below becomes the sole active version
        conn.execute(
            "UPDATE results SET active=0 WHERE tournament_id=? AND match_id=? "
            "AND active=1",
            (tournament_id, target_match_id),
        )
        version = db.max_result_version(conn, tournament_id, target_match_id) + 1
        conn.execute(
            "INSERT INTO results(tournament_id, match_id, version, active, winner, "
            "loser, score_a, score_b, note) VALUES (?,?,?,1,?,?,?,?,?)",
            (tournament_id, target_match_id, version, new_winner,
             sim["new_loser"], target_row["score_a"], target_row["score_b"],
             (target_row["note"] or "") + " [corrected]"),
        )
        # Void the affected downstream matches: deactivate ALL their active
        # rows so no historical version keeps propagating.
        for mid in sim["voided"]:
            conn.execute(
                "UPDATE results SET active=0 WHERE tournament_id=? AND match_id=? "
                "AND active=1",
                (tournament_id, mid),
            )
        conn.execute(
            "UPDATE previews SET consumed=1 WHERE token=?", (payload.preview_token,)
        )

        name = conn.execute("SELECT name FROM tournaments WHERE id=?",
                            (tournament_id,)).fetchone()["name"]
        results = active_result_dicts(db.active_results(conn, tournament_id))
        views = build_match_views(match_rows, results)
    return TournamentView(tournament_id=tournament_id, name=name, matches=views)
