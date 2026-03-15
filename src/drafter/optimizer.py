"""Draft optimizer — z-score rankings, positional scarcity, category need analysis."""

from __future__ import annotations

import math
from dataclasses import dataclass

from .config import LeagueConfig
from .models import Player


@dataclass
class Recommendation:
    player: Player
    total_score: float
    z_score_value: float
    scarcity_bonus: float
    need_bonus: float
    reasoning: str


class Optimizer:
    def __init__(self, config: LeagueConfig):
        self.config = config

    def recommend(
        self,
        available: list[Player],
        my_roster: list[Player],
        all_players: list[Player],
        n: int = 10,
        pool: str | None = None,
    ) -> list[Recommendation]:
        """Recommend the best available picks considering value, scarcity, and need.

        Args:
            pool: "hitter" or "pitcher" to restrict to that pool.
                  Since hitters and pitchers are drafted separately, this should
                  always be specified. If None, uses all players (legacy behavior).
        """
        if not available:
            return []

        # Filter to the correct pool
        if pool == "hitter":
            available = [p for p in available if p.is_hitter]
        elif pool == "pitcher":
            available = [p for p in available if p.is_pitcher]

        if not available:
            return []

        # Compute z-scores for available players across relevant categories
        z_scores = self._compute_z_scores(available, pool=pool)

        # Compute positional scarcity
        scarcity = self._compute_scarcity(available, pool=pool)

        # Compute category needs based on current roster
        needs = self._compute_category_needs(my_roster, available, pool=pool)

        recs = []
        for player in available:
            if player.name not in z_scores:
                continue

            z_val = z_scores[player.name]
            scar = self._player_scarcity_bonus(player, scarcity)
            need = self._player_need_bonus(player, needs)

            total = z_val + scar + need
            reasoning = self._build_reasoning(player, z_val, scar, need, needs)

            recs.append(Recommendation(
                player=player,
                total_score=round(total, 2),
                z_score_value=round(z_val, 2),
                scarcity_bonus=round(scar, 2),
                need_bonus=round(need, 2),
                reasoning=reasoning,
            ))

        recs.sort(key=lambda r: r.total_score, reverse=True)
        return recs[:n]

    def _compute_z_scores(
        self, players: list[Player], pool: str | None = None
    ) -> dict[str, float]:
        """Compute aggregate z-score value for each player across scoring categories."""
        if pool == "hitter":
            categories = self.config.hitting_categories
        elif pool == "pitcher":
            categories = self.config.pitching_categories
        else:
            categories = self.config.all_categories

        cat_values: dict[str, list[tuple[str, float]]] = {
            cat: [] for cat in categories
        }

        for p in players:
            if pool != "pitcher":
                for cat in self.config.hitting_categories:
                    if cat in categories:
                        val = p.hitting_projections.get(cat)
                        if val is not None:
                            cat_values[cat].append((p.name, val))

            if pool != "hitter":
                for cat in self.config.pitching_categories:
                    if cat in categories:
                        val = p.pitching_projections.get(cat)
                        if val is not None:
                            cat_values[cat].append((p.name, val))

        # Compute z-scores per category
        player_z: dict[str, float] = {}
        for cat, values in cat_values.items():
            if len(values) < 2:
                continue

            vals = [v for _, v in values]
            mean = sum(vals) / len(vals)
            variance = sum((v - mean) ** 2 for v in vals) / len(vals)
            std = math.sqrt(variance) if variance > 0 else 1

            inverse = cat in self.config.inverse_categories

            for name, val in values:
                z = (val - mean) / std
                if inverse:
                    z = -z  # Lower is better for ERA, WHIP

                player_z[name] = player_z.get(name, 0) + z

        return player_z

    def _compute_scarcity(
        self, available: list[Player], pool: str | None = None
    ) -> dict[str, float]:
        """Compute scarcity score for each position based on available talent."""
        pos_counts: dict[str, int] = {}
        roster = self.config.roster_slots

        hitting_positions = {"C", "1B", "2B", "SS", "3B", "IF", "LF", "CF", "RF", "OF", "DH"}
        pitching_positions = {"SP", "RP"}

        for p in available:
            for pos in p.positions:
                pos_counts[pos] = pos_counts.get(pos, 0) + 1

        # Scarcity = how few quality options remain relative to roster need
        scarcity = {}
        for pos, slots in roster.items():
            if pos == "Bench":
                continue
            # Skip positions not relevant to the current pool
            if pool == "hitter" and pos not in hitting_positions:
                continue
            if pool == "pitcher" and pos not in pitching_positions:
                continue
            count = pos_counts.get(pos, 0)
            if pos == "IF":
                count = sum(pos_counts.get(p, 0) for p in ("C", "1B", "2B", "SS", "3B"))
            elif pos == "OF":
                count = sum(pos_counts.get(p, 0) for p in ("LF", "CF", "RF", "OF"))

            # Ratio of available players per needed slot (across all teams)
            needed_league = slots * self.config.num_teams
            ratio = count / needed_league if needed_league > 0 else 99
            scarcity[pos] = max(0, 2 - ratio)  # Higher = more scarce

        return scarcity

    def _player_scarcity_bonus(
        self, player: Player, scarcity: dict[str, float]
    ) -> float:
        """Scarcity bonus for a player based on their most scarce eligible position."""
        if not player.positions:
            return 0
        return max(scarcity.get(pos, 0) for pos in player.positions) * 0.5

    def _compute_category_needs(
        self, my_roster: list[Player], available: list[Player],
        pool: str | None = None,
    ) -> dict[str, float]:
        """Score how much each category needs help (higher = greater need)."""
        if pool == "hitter":
            categories = self.config.hitting_categories
        elif pool == "pitcher":
            categories = self.config.pitching_categories
        else:
            categories = self.config.all_categories

        if not my_roster:
            return {cat: 1.0 for cat in categories}

        # Sum current roster projections per category
        totals: dict[str, float] = {}
        if pool != "pitcher":
            for cat in self.config.hitting_categories:
                if cat in categories:
                    totals[cat] = sum(
                        p.hitting_projections.get(cat, 0) for p in my_roster if p.is_hitter
                    )
        if pool != "hitter":
            for cat in self.config.pitching_categories:
                if cat in categories:
                    totals[cat] = sum(
                        p.pitching_projections.get(cat, 0) for p in my_roster if p.is_pitcher
                    )

        # Compare to what a "target" roster would have
        # Use average of top-N available as benchmark
        n_bench = max(5, len(available) // self.config.num_teams)
        top_avail = sorted(available, key=lambda p: p.adp)[:n_bench]

        target: dict[str, float] = {}
        if pool != "pitcher":
            for cat in self.config.hitting_categories:
                if cat in categories:
                    vals = [p.hitting_projections.get(cat, 0) for p in top_avail if p.is_hitter]
                    target[cat] = sum(vals) / max(len(vals), 1)
        if pool != "hitter":
            for cat in self.config.pitching_categories:
                if cat in categories:
                    vals = [p.pitching_projections.get(cat, 0) for p in top_avail if p.is_pitcher]
                    target[cat] = sum(vals) / max(len(vals), 1)

        # Need score: how far behind the average contribution we are
        needs = {}
        for cat in categories:
            if target.get(cat, 0) == 0:
                needs[cat] = 1.0
                continue

            current_per_player = totals.get(cat, 0) / max(len(my_roster), 1)
            ratio = current_per_player / target[cat] if target[cat] != 0 else 1

            if cat in self.config.inverse_categories:
                # For ERA/WHIP: higher current value = more need
                needs[cat] = max(0, ratio - 0.8) * 2
            else:
                # For counting stats: lower ratio = more need
                needs[cat] = max(0, 1.2 - ratio) * 2

        return needs

    def _player_need_bonus(
        self, player: Player, needs: dict[str, float]
    ) -> float:
        """Bonus for a player based on how well they fill category needs."""
        bonus = 0
        for cat in self.config.hitting_categories:
            val = player.hitting_projections.get(cat, 0)
            if val and needs.get(cat, 0) > 0.5:
                bonus += needs[cat] * 0.3

        for cat in self.config.pitching_categories:
            val = player.pitching_projections.get(cat, 0)
            if val and needs.get(cat, 0) > 0.5:
                bonus += needs[cat] * 0.3

        return bonus

    def _build_reasoning(
        self,
        player: Player,
        z_val: float,
        scar: float,
        need: float,
        needs: dict[str, float],
    ) -> str:
        parts = []

        # Value assessment
        if z_val > 3:
            parts.append("Elite value")
        elif z_val > 1.5:
            parts.append("Strong value")
        elif z_val > 0:
            parts.append("Solid value")

        # Scarcity
        if scar > 0.5:
            scarce_pos = [
                pos for pos in player.positions
                if pos not in ("OF", "DH", "Bench")
            ]
            if scarce_pos:
                parts.append(f"scarce position ({'/'.join(scarce_pos)})")

        # Category strengths
        strengths = []
        if player.is_hitter:
            proj = player.hitting_projections
            if proj.get("HR", 0) > 30:
                strengths.append("power")
            if proj.get("SB", 0) > 20:
                strengths.append("speed")
            if proj.get("AVG", 0) > 0.290:
                strengths.append("high AVG")
            if proj.get("R", 0) > 90:
                strengths.append("runs")
            if proj.get("RBI", 0) > 90:
                strengths.append("RBI")
        if player.is_pitcher:
            proj = player.pitching_projections
            if proj.get("ERA", 99) < 3.0:
                strengths.append("elite ERA")
            if proj.get("K", 0) > 200:
                strengths.append("high K")
            if proj.get("QS", 0) > 20:
                strengths.append("quality starts")
            if proj.get("SV", 0) > 25:
                strengths.append("saves")
            if proj.get("WHIP", 99) < 1.05:
                strengths.append("low WHIP")

        if strengths:
            parts.append(f"excels in {', '.join(strengths)}")

        # Need fit
        if need > 1.5:
            top_needs = sorted(
                [(cat, n) for cat, n in needs.items() if n > 0.5],
                key=lambda x: x[1],
                reverse=True,
            )[:3]
            need_cats = [cat for cat, _ in top_needs]
            parts.append(f"fills needs in {', '.join(need_cats)}")

        return ". ".join(parts) if parts else "Available value pick"

    def analyze_roster(self, roster: list[Player]) -> dict[str, float]:
        """Analyze projected category totals for a roster."""
        totals: dict[str, float] = {}
        for cat in self.config.hitting_categories:
            totals[cat] = round(
                sum(p.hitting_projections.get(cat, 0) for p in roster if p.is_hitter), 2
            )
        for cat in self.config.pitching_categories:
            if cat in self.config.inverse_categories:
                # Weighted average by IP for rate stats
                total_ip = sum(
                    p.pitching_projections.get("IP", 0) for p in roster if p.is_pitcher
                )
                if total_ip > 0:
                    if cat == "ERA":
                        total_er = sum(
                            p.pitching_projections.get("ER", 0)
                            for p in roster if p.is_pitcher
                        )
                        totals[cat] = round(total_er * 9 / total_ip, 3)
                    elif cat == "WHIP":
                        total_hw = sum(
                            (p.pitching_projections.get("H", 0) + p.pitching_projections.get("BB", 0))
                            for p in roster if p.is_pitcher
                        )
                        totals[cat] = round(total_hw / total_ip, 3)
                else:
                    totals[cat] = 0
            else:
                totals[cat] = round(
                    sum(p.pitching_projections.get(cat, 0) for p in roster if p.is_pitcher), 2
                )
        return totals
