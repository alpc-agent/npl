"""League configuration."""

from __future__ import annotations

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

    # Per-category weight multipliers for strategy slanting.
    # Empty dict = all 1.0 (balanced). Values < 0.5 suppress need-chasing.
    category_weights: dict[str, float] = field(default_factory=dict)

    # League-specific ADP adjustments: position -> round shift.
    # Positive = league drafts this position later than consensus (e.g. {"RP": 2.0}).
    # Used by pick_safety() to discount threat probabilities for delayed positions.
    league_adp_adjustments: dict[str, float] = field(default_factory=dict)

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

    def weight(self, cat: str) -> float:
        """Get the weight multiplier for a category (default 1.0)."""
        return self.category_weights.get(cat, 1.0)

    @classmethod
    def with_strategy(cls, strategy: str, **kwargs) -> LeagueConfig:
        """Create a config with preset category weights.

        Strategies: balanced, punt_sb, punt_sv, punt_avg
        """
        presets = {
            "balanced": {},
            "punt_sb": {"SB": 0.1},
            "punt_sv": {"SV": 0.1},
            "punt_avg": {"AVG": 0.2},
        }
        weights = presets.get(strategy)
        if weights is None:
            raise ValueError(f"Unknown strategy: {strategy!r}. Options: {list(presets)}")
        return cls(category_weights=weights, **kwargs)
