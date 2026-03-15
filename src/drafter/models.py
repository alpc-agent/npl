"""Data models for draft tracking."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path


@dataclass
class Player:
    name: str
    player_id: str
    team: str
    age: int
    positions: list[str]
    hitting_projections: dict[str, float] = field(default_factory=dict)
    pitching_projections: dict[str, float] = field(default_factory=dict)
    adp: float = 999.0
    tags: list[str] = field(default_factory=list)

    @property
    def is_hitter(self) -> bool:
        return any(p not in ("SP", "RP") for p in self.positions)

    @property
    def is_pitcher(self) -> bool:
        return any(p in ("SP", "RP") for p in self.positions)

    def __str__(self) -> str:
        pos = "/".join(self.positions)
        tag_str = " " + " ".join(f"[{t.upper()}]" for t in self.tags) if self.tags else ""
        return f"{self.name} ({pos}, {self.team}) ADP: {self.adp:.0f}{tag_str}"


@dataclass
class DraftPick:
    pick_number: int
    round_number: int
    player_name: str
    player_id: str
    team_name: str


@dataclass
class DraftState:
    picks: list[DraftPick] = field(default_factory=list)
    num_teams: int = 12
    draft_type: str = "snake"
    my_team: str | None = None
    my_draft_position: int | None = None
    team_names: list[str] = field(default_factory=list)

    @property
    def current_pick(self) -> int:
        return len(self.picks) + 1

    @property
    def current_round(self) -> int:
        return (self.current_pick - 1) // self.num_teams + 1

    def picking_team(self, pick_number: int | None = None) -> str:
        """Which team picks at a given pick number (snake draft)."""
        pick = pick_number or self.current_pick
        round_num = (pick - 1) // self.num_teams + 1
        pos_in_round = (pick - 1) % self.num_teams

        if self.draft_type == "snake" and round_num % 2 == 0:
            pos_in_round = self.num_teams - 1 - pos_in_round

        if self.team_names:
            return self.team_names[pos_in_round]
        return f"Team {pos_in_round + 1}"

    def is_my_pick(self, pick_number: int | None = None) -> bool:
        return self.picking_team(pick_number) == self.my_team

    def picks_until_mine(self) -> int | None:
        """How many picks until my next turn."""
        for i in range(self.current_pick, self.current_pick + 2 * self.num_teams):
            if self.is_my_pick(i):
                return i - self.current_pick
        return None

    def drafted_player_ids(self) -> set[str]:
        return {p.player_id for p in self.picks}

    def team_picks(self, team_name: str) -> list[DraftPick]:
        return [p for p in self.picks if p.team_name == team_name]

    def picks_before_mine(self) -> list[tuple[int, str]]:
        """Return (pick_number, team_name) for every pick between now and my next pick.

        Excludes my own pick. Returns empty list if it's currently my pick.
        """
        result = []
        for i in range(self.current_pick, self.current_pick + 2 * self.num_teams):
            team = self.picking_team(i)
            if team == self.my_team:
                break
            result.append((i, team))
        return result

    def save(self, path: Path) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, indent=2)

    @classmethod
    def load(cls, path: Path) -> DraftState:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        state = cls(
            num_teams=data["num_teams"],
            draft_type=data["draft_type"],
            my_team=data["my_team"],
            my_draft_position=data["my_draft_position"],
            team_names=data["team_names"],
        )
        state.picks = [DraftPick(**p) for p in data["picks"]]
        return state
