"""Draft optimizer — z-score rankings, positional scarcity, tier analysis."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .config import LeagueConfig
from .models import Player


@dataclass
class TierInfo:
    position: str
    tier: int
    total_in_tier: int
    remaining_in_tier: int

    @property
    def tier_label(self) -> str:
        return f"Tier {self.tier} {self.position} ({self.remaining_in_tier} left)"


@dataclass
class Recommendation:
    player: Player
    total_score: float
    z_score_value: float
    scarcity_bonus: float
    need_bonus: float
    reasoning: str
    adp_rank: int = 0  # Mr. Cheatsheet's objective rank (by ADP among available)
    tiers: list[TierInfo] = field(default_factory=list)

    @property
    def best_tier(self) -> TierInfo | None:
        if not self.tiers:
            return None
        return min(self.tiers, key=lambda t: t.tier)


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
        """Recommend the best available picks considering value, scarcity, need, and tiers."""
        if not available:
            return []

        if pool == "hitter":
            available = [p for p in available if p.is_hitter]
        elif pool == "pitcher":
            available = [p for p in available if p.is_pitcher]

        if not available:
            return []

        z_scores = self._compute_z_scores(available, pool=pool)
        scarcity = self._compute_scarcity(available, pool=pool)
        needs = self._compute_category_needs(my_roster, available, pool=pool)
        tier_map = self._compute_tiers(available, z_scores, pool=pool)

        # Compute ADP rank (Mr. Cheatsheet's objective ordering among available)
        adp_sorted = sorted(available, key=lambda p: p.adp)
        adp_rank_map = {p.name: rank + 1 for rank, p in enumerate(adp_sorted)}

        recs = []
        for player in available:
            if player.name not in z_scores:
                continue

            z_val = z_scores[player.name]
            scar = self._player_scarcity_bonus(player, scarcity)
            need = self._player_need_bonus(player, needs)
            player_tiers = tier_map.get(player.name, [])

            # Tier-aware scarcity: boost if player is in a nearly-depleted elite tier
            tier_urgency = self._tier_urgency_bonus(player_tiers)

            total = z_val + scar + need + tier_urgency
            reasoning = self._build_reasoning(
                player, z_val, scar, need, needs, player_tiers
            )

            recs.append(Recommendation(
                player=player,
                total_score=round(total, 2),
                z_score_value=round(z_val, 2),
                scarcity_bonus=round(scar, 2),
                need_bonus=round(need, 2),
                reasoning=reasoning,
                adp_rank=adp_rank_map.get(player.name, 999),
                tiers=player_tiers,
            ))

        # Sort by total score, then by best tier (lower = better) as tiebreaker
        recs.sort(key=lambda r: (
            r.total_score,
            -(r.best_tier.tier if r.best_tier else 99),
        ), reverse=True)
        return recs[:n]

    def _compute_z_scores(
        self, players: list[Player], pool: str | None = None
    ) -> dict[str, float]:
        """Compute z-scores with rate-stat weighting by playing time."""
        if pool == "hitter":
            categories = self.config.hitting_categories
        elif pool == "pitcher":
            categories = self.config.pitching_categories
        else:
            categories = self.config.all_categories

        # For rate stats, we compute "marginal contribution above average"
        # AVG: weighted by AB -> (AVG - mean_AVG) * AB, then z-score that
        # ERA/WHIP: weighted by IP -> (mean - val) * IP, then z-score that
        rate_cats_hitting = {"AVG"}
        rate_cats_pitching = {"ERA", "WHIP"}

        # Collect raw values with volume weights
        cat_values: dict[str, list[tuple[str, float]]] = {
            cat: [] for cat in categories
        }

        for p in players:
            if pool != "pitcher":
                ab = p.hitting_projections.get("AB", 0)
                for cat in self.config.hitting_categories:
                    if cat not in categories:
                        continue
                    val = p.hitting_projections.get(cat)
                    if val is None:
                        continue
                    if cat in rate_cats_hitting and ab > 0:
                        # Store (name, rate, volume) — we'll convert after computing mean
                        cat_values[cat].append((p.name, val, ab))
                    else:
                        cat_values[cat].append((p.name, val, None))

            if pool != "hitter":
                ip = p.pitching_projections.get("IP", 0)
                for cat in self.config.pitching_categories:
                    if cat not in categories:
                        continue
                    val = p.pitching_projections.get(cat)
                    if val is None:
                        continue
                    if cat in rate_cats_pitching and ip > 0:
                        cat_values[cat].append((p.name, val, ip))
                    else:
                        cat_values[cat].append((p.name, val, None))

        player_z: dict[str, float] = {}

        for cat, values in cat_values.items():
            if len(values) < 2:
                continue

            inverse = cat in self.config.inverse_categories
            is_rate = cat in rate_cats_hitting or cat in rate_cats_pitching

            if is_rate:
                # Filter to players with volume data
                weighted = [(n, v, vol) for n, v, vol in values if vol and vol > 0]
                if len(weighted) < 2:
                    continue

                # Compute pool mean rate
                rates = [v for _, v, _ in weighted]
                mean_rate = sum(rates) / len(rates)

                # Marginal contribution: (rate - mean) * volume
                # For inverse stats: (mean - rate) * volume (lower is better)
                contribs = []
                for name, rate, vol in weighted:
                    if inverse:
                        contribs.append((name, (mean_rate - rate) * vol))
                    else:
                        contribs.append((name, (rate - mean_rate) * vol))

                # Z-score the contributions
                vals = [c for _, c in contribs]
                mean_c = sum(vals) / len(vals)
                variance = sum((v - mean_c) ** 2 for v in vals) / len(vals)
                std = math.sqrt(variance) if variance > 0 else 1

                for name, contrib in contribs:
                    z = (contrib - mean_c) / std
                    player_z[name] = player_z.get(name, 0) + z
            else:
                # Counting stats: standard z-score
                vals = [v for _, v, _ in values]
                mean = sum(vals) / len(vals)
                variance = sum((v - mean) ** 2 for v in vals) / len(vals)
                std = math.sqrt(variance) if variance > 0 else 1

                for name, val, _ in values:
                    z = (val - mean) / std
                    if inverse:
                        z = -z
                    player_z[name] = player_z.get(name, 0) + z

        return player_z

    def _compute_tiers(
        self,
        available: list[Player],
        z_scores: dict[str, float],
        pool: str | None = None,
    ) -> dict[str, list[TierInfo]]:
        """Compute position-based tiers for available players.

        Players appear in tiers for EVERY position they're eligible for.
        Uses fixed-count tiers: tier_size = num_teams for single-slot positions,
        scaled by roster slots for multi-slot positions (e.g., SP has 5 slots).
        """
        if pool == "hitter":
            positions = {"C", "1B", "2B", "SS", "3B", "LF", "CF", "RF"}
        elif pool == "pitcher":
            positions = {"SP", "RP"}
        else:
            positions = {"C", "1B", "2B", "SS", "3B", "LF", "CF", "RF", "SP", "RP"}

        # Group players by position (a player can appear in multiple groups)
        pos_players: dict[str, list[tuple[str, float]]] = {}
        for pos in positions:
            players_at_pos = []
            for p in available:
                if p.name not in z_scores:
                    continue
                if pos in p.positions:
                    players_at_pos.append((p.name, z_scores[p.name]))
                # Also handle OF eligibility for LF/CF/RF
                elif pos in ("LF", "CF", "RF") and "OF" in p.positions:
                    players_at_pos.append((p.name, z_scores[p.name]))
            if players_at_pos:
                # Sort by z-score descending
                players_at_pos.sort(key=lambda x: x[1], reverse=True)
                pos_players[pos] = players_at_pos

        # Determine tier size per position
        # Tier 1 = elite starters (roughly 1 per team)
        # For multi-slot positions (SP=5), tier 1 is still ~12 (the aces),
        # not 60 (all starters). Tiers represent talent clusters, not roster fill.
        tier_sizes: dict[str, int] = {}
        for pos in positions:
            tier_sizes[pos] = self.config.num_teams  # 12 per tier

        # Assign tiers
        result: dict[str, list[TierInfo]] = {}
        for pos, players_list in pos_players.items():
            tier_size = tier_sizes.get(pos, 12)
            total_at_pos = len(players_list)

            for rank, (name, _z) in enumerate(players_list):
                tier_num = rank // tier_size + 1
                # Count remaining in this tier
                tier_start = (tier_num - 1) * tier_size
                tier_end = min(tier_num * tier_size, total_at_pos)
                total_in_tier = tier_end - tier_start

                info = TierInfo(
                    position=pos,
                    tier=tier_num,
                    total_in_tier=total_in_tier,
                    remaining_in_tier=total_in_tier,  # All remaining since they're available
                )

                if name not in result:
                    result[name] = []
                result[name].append(info)

        return result

    def _tier_urgency_bonus(self, tiers: list[TierInfo]) -> float:
        """Bonus for players in nearly-depleted elite tiers."""
        if not tiers:
            return 0

        bonus = 0
        for t in tiers:
            if t.tier > 2:
                continue
            # Urgency increases as tier depletes
            # Tier 1 with <=3 remaining: strong signal
            # Tier 2 with <=3 remaining: moderate signal
            if t.remaining_in_tier <= 3:
                tier_weight = 1.0 if t.tier == 1 else 0.5
                depletion = (4 - t.remaining_in_tier) / 3  # 0.33 to 1.0
                bonus = max(bonus, tier_weight * depletion * 0.8)

        return bonus

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

        scarcity = {}
        for pos, slots in roster.items():
            if pos == "Bench":
                continue
            if pool == "hitter" and pos not in hitting_positions:
                continue
            if pool == "pitcher" and pos not in pitching_positions:
                continue
            count = pos_counts.get(pos, 0)
            if pos == "IF":
                count = sum(pos_counts.get(p, 0) for p in ("C", "1B", "2B", "SS", "3B"))
            elif pos == "OF":
                count = sum(pos_counts.get(p, 0) for p in ("LF", "CF", "RF", "OF"))

            needed_league = slots * self.config.num_teams
            ratio = count / needed_league if needed_league > 0 else 99
            scarcity[pos] = max(0, 2 - ratio)

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

        needs = {}
        for cat in categories:
            if target.get(cat, 0) == 0:
                needs[cat] = 1.0
                continue

            current_per_player = totals.get(cat, 0) / max(len(my_roster), 1)
            ratio = current_per_player / target[cat] if target[cat] != 0 else 1

            if cat in self.config.inverse_categories:
                needs[cat] = max(0, ratio - 0.8) * 2
            else:
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
        tiers: list[TierInfo],
    ) -> str:
        parts = []

        # Value assessment
        if z_val > 3:
            parts.append("Elite value")
        elif z_val > 1.5:
            parts.append("Strong value")
        elif z_val > 0:
            parts.append("Solid value")

        # Tier info — show best tier and depletion warning
        if tiers:
            best = min(tiers, key=lambda t: t.tier)
            parts.append(best.tier_label)
            # Depletion warning
            urgent = [t for t in tiers if t.tier <= 2 and t.remaining_in_tier <= 3]
            if urgent:
                most_urgent = min(urgent, key=lambda t: (t.tier, t.remaining_in_tier))
                if most_urgent.remaining_in_tier <= 2:
                    parts.append(f"REACH: only {most_urgent.remaining_in_tier} left in {most_urgent.position} Tier {most_urgent.tier}")

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
