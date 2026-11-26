"""Pydantic request/response models for the tournament reversal API."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

SlotSide = Literal["team", "winner", "loser"]
SlotState = Literal["team", "winner", "loser", "tbd"]


class SlotIn(BaseModel):
    type: SlotSide
    team: Optional[str] = Field(default=None, max_length=100)
    match_id: Optional[int] = Field(default=None, ge=1)


class MatchIn(BaseModel):
    slot_a: SlotIn
    slot_b: SlotIn


class TournamentIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    matches: list[MatchIn] = Field(min_length=1, max_length=32)


class SlotView(BaseModel):
    state: SlotState
    team: Optional[str] = None
    source_match_id: Optional[int] = None


class ResultView(BaseModel):
    active: bool
    winner: str
    loser: str
    score_a: int
    score_b: int
    note: Optional[str] = None
    version: int


class VoidedResultView(ResultView):
    match_id: int


class MatchView(BaseModel):
    match_id: int
    slot_a: SlotView
    slot_b: SlotView
    determined: bool
    teams: Optional[list[str]] = None
    result: Optional[ResultView] = None


class TournamentView(BaseModel):
    tournament_id: int
    name: str
    matches: list[MatchView]


class ScoreIn(BaseModel):
    winner: str = Field(min_length=1, max_length=100)
    score_a: int = Field(ge=0)
    score_b: int = Field(ge=0)
    note: Optional[str] = Field(default=None, max_length=200)


class CorrectionPreviewIn(BaseModel):
    match_id: int
    new_winner: str = Field(min_length=1, max_length=100)


class CorrectedMatchView(BaseModel):
    match_id: int
    new_teams: list[str]
    new_winner: str


class PreviewView(BaseModel):
    preview_token: str
    target_match_id: int
    new_winner: str
    corrected_match: CorrectedMatchView
    voided_match_ids: list[int]
    voided_results: list[VoidedResultView]
    retained_match_ids: list[int]
    projected_matches: list[MatchView]


class CommitIn(BaseModel):
    preview_token: str = Field(min_length=8, max_length=64)
