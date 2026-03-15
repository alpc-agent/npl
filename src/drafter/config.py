"""League configuration."""

from dataclasses import dataclass, field


@dataclass
class LeagueConfig:
    num_teams: int = 12
    draft_type: str = "snake"

    hitting_categories: list[str] = field(
        default_factory=lambda: ["AVG", "HR", "R", "RBI", "SB"]
    )
    pitching_categories: list[str] = field(
        default_factory=lambda: ["QS", "SV", "K", "ERA", "WHIP"]
    )

    # Categories where lower is better
    inverse_categories: list[str] = field(
        default_factory=lambda: ["ERA", "WHIP"]
    )

    roster_slots: dict[str, int] = field(
        default_factory=lambda: {
            "C": 1,
            "1B": 1,
            "2B": 1,
            "SS": 1,
            "3B": 1,
            "IF": 1,   # Any infielder (C/1B/2B/SS/3B)
            "LF": 1,
            "CF": 1,
            "RF": 1,
            "OF": 1,   # Any outfielder (LF/CF/RF)
            "DH": 1,
            "SP": 5,
            "RP": 2,
        }
    )

    @property
    def all_categories(self) -> list[str]:
        return self.hitting_categories + self.pitching_categories

    @property
    def total_roster_size(self) -> int:
        return sum(self.roster_slots.values())
