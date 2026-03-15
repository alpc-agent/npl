"""Draft optimizer — z-score rankings, positional scarcity, tier analysis."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

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
class PickSafety:
    """Pick safety analysis for a position."""
    position: str
    viable_in_tier: int       # Players remaining in the current target tier
    picks_before_turn: int    # Total picks before user's next turn
    teams_needing: int        # Teams picking before user that haven't filled this position
    prob_available: float     # Probability at least one tier player survives to user's pick
    signal: str               # "safe", "monitor", or "reach"
    detail: str               # Human-readable explanation


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
    tags: list[str] = field(default_factory=list)
    rank_insight: str = ""  # Why AI rank differs from cheatsheet rank
    safety_flags: list[PickSafety] = field(default_factory=list)  # Position safety signals

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
        recs = recs[:n]

        # Assign tags and rank insights
        for i, r in enumerate(recs, 1):
            r.tags = list(r.player.tags)
            if r.adp_rank >= i + 15:
                r.tags.append("value")
            r.rank_insight = self._rank_insight(r, ai_rank=i)

        return recs

    def annotate_safety(
        self,
        recs: list[Recommendation],
        safety: list[PickSafety],
    ) -> None:
        """Attach pick safety flags to recommendations.

        For each recommendation, find safety signals matching the player's positions
        and attach them. Also adds 'safe' or 'reach' to tags when relevant.
        """
        safety_by_pos = {s.position: s for s in safety}

        for r in recs:
            seen = set()
            matched = []
            for pos in r.player.positions:
                if pos in safety_by_pos and pos not in seen:
                    matched.append(safety_by_pos[pos])
                    seen.add(pos)
                # Check OF umbrella positions
                if pos == "OF":
                    for sub in ("LF", "CF", "RF"):
                        if sub in safety_by_pos and sub not in seen:
                            matched.append(safety_by_pos[sub])
                            seen.add(sub)

            r.safety_flags = matched

            # Add safety tag based on the most urgent signal for this player's positions
            if matched:
                most_urgent = min(matched, key=lambda s: s.prob_available)
                if most_urgent.signal == "reach" and "reach" not in r.tags:
                    r.tags.append("reach")
                elif most_urgent.signal == "safe" and most_urgent.teams_needing == 0:
                    if "safe" not in r.tags:
                        r.tags.append("safe")

            # WAIT/SNIPE tags for positions with league ADP adjustments (e.g. RP).
            # WAIT: this player's position is safe, you can delay.
            # SNIPE: elite player at a league-discounted position — grab the value.
            if matched and self.config.league_adp_adjustments:
                for sf in matched:
                    adj = self.config.league_adp_adjustments.get(sf.position, 0)
                    if adj <= 0:
                        continue
                    if sf.signal == "safe" and "wait" not in r.tags:
                        r.tags.append("wait")
                    elif sf.signal == "monitor" and r.adp_rank <= 5:
                        if "snipe" not in r.tags:
                            r.tags.append("snipe")

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
                    player_z[name] = player_z.get(name, 0) + z * self.config.weight(cat)
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
                    player_z[name] = player_z.get(name, 0) + z * self.config.weight(cat)

        return player_z

    def _compute_tiers(
        self,
        available: list[Player],
        z_scores: dict[str, float],
        pool: str | None = None,
    ) -> dict[str, list[TierInfo]]:
        """Compute position-based tiers using natural gap detection.

        Players appear in tiers for EVERY position they're eligible for.
        Tiers are determined by finding significant z-score gaps between
        consecutive players at each position — a tier break occurs when the
        drop-off exceeds a threshold relative to the overall spread.
        """
        if pool == "hitter":
            positions = {"C", "1B", "2B", "SS", "3B", "LF", "CF", "RF"}
        elif pool == "pitcher":
            positions = {"SP", "RP"}
        else:
            positions = {"C", "1B", "2B", "SS", "3B", "LF", "CF", "RF", "SP", "RP"}

        # Limit tier analysis to draft-relevant depth per position
        # (roster_slot * num_teams * 3 gives ~3x the draftable range)
        roster = self.config.roster_slots

        # Group players by position (a player can appear in multiple groups)
        pos_players: dict[str, list[tuple[str, float]]] = {}
        for pos in positions:
            players_at_pos = []
            for p in available:
                if p.name not in z_scores:
                    continue
                if pos in p.positions:
                    players_at_pos.append((p.name, z_scores[p.name]))
                elif pos in ("LF", "CF", "RF") and "OF" in p.positions:
                    players_at_pos.append((p.name, z_scores[p.name]))
            if players_at_pos:
                players_at_pos.sort(key=lambda x: x[1], reverse=True)
                # Cap to draft-relevant depth (starters + bench buffer)
                slots = roster.get(pos, 1)
                cap = max((slots + 3) * self.config.num_teams, 60)
                pos_players[pos] = players_at_pos[:cap]

        # Assign tiers using natural gap detection
        result: dict[str, list[TierInfo]] = {}
        for pos, players_list in pos_players.items():
            tier_assignments = self._find_natural_tiers(players_list)

            for name, tier_num, tier_count in tier_assignments:
                info = TierInfo(
                    position=pos,
                    tier=tier_num,
                    total_in_tier=tier_count,
                    remaining_in_tier=tier_count,
                )

                if name not in result:
                    result[name] = []
                result[name].append(info)

        return result

    def _find_natural_tiers(
        self, players: list[tuple[str, float]], max_tiers: int = 15
    ) -> list[tuple[str, int, int]]:
        """Find natural tier breaks in a sorted list of (name, z_score) tuples.

        Uses top-N gap detection: tier breaks are placed at the largest
        z-score drops between consecutive players. This guarantees up to
        max_tiers tiers, with breaks at the most significant natural gaps.

        Returns list of (name, tier_number, players_in_tier).
        """
        if not players:
            return []
        if len(players) == 1:
            return [(players[0][0], 1, 1)]

        # Compute gaps between consecutive players
        gaps = []
        for i in range(len(players) - 1):
            gap = players[i][1] - players[i + 1][1]
            gaps.append((i, gap))

        # Find the top (max_tiers - 1) largest gaps as tier break points
        n_breaks = min(max_tiers - 1, len(gaps))
        sorted_by_size = sorted(gaps, key=lambda x: x[1], reverse=True)
        break_indices = sorted(idx for idx, _ in sorted_by_size[:n_breaks])

        # Assign tiers
        tier_num = 1
        assignments: list[tuple[str, int]] = [(players[0][0], 1)]

        for i in range(len(players) - 1):
            if break_indices and i == break_indices[0]:
                tier_num += 1
                break_indices.pop(0)
            assignments.append((players[i + 1][0], tier_num))

        # Count players per tier and build result
        tier_counts: dict[int, int] = {}
        for _, t in assignments:
            tier_counts[t] = tier_counts.get(t, 0) + 1

        return [(name, tier, tier_counts[tier]) for name, tier in assignments]

    def _tier_urgency_bonus(self, tiers: list[TierInfo]) -> float:
        """Bonus for players in nearly-depleted elite tiers.

        Uses percentage-based depletion since tier sizes vary naturally.
        """
        if not tiers:
            return 0

        bonus = 0
        for t in tiers:
            if t.tier > 2:
                continue
            # Urgency: fewer remaining = more urgent
            # For small tiers (1-3 players), any remaining triggers urgency
            if t.remaining_in_tier <= 3:
                tier_weight = 1.0 if t.tier == 1 else 0.5
                depletion = (4 - t.remaining_in_tier) / 3  # 0.33 to 1.0
                bonus = max(bonus, tier_weight * depletion * 0.8)
            elif t.total_in_tier > 3 and t.remaining_in_tier <= t.total_in_tier * 0.25:
                # Larger tier nearly depleted (< 25% remaining)
                tier_weight = 1.0 if t.tier == 1 else 0.5
                pct_gone = 1 - (t.remaining_in_tier / t.total_in_tier)
                bonus = max(bonus, tier_weight * pct_gone * 0.6)

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

        # Suppress needs for punted categories
        for cat in categories:
            if self.config.weight(cat) < 0.5:
                needs[cat] = 0

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

    def _rank_insight(self, rec: Recommendation, ai_rank: int) -> str:
        """Explain why AI rank differs from cheatsheet rank."""
        diff = rec.adp_rank - ai_rank
        if abs(diff) < 5:
            return ""

        if diff > 0:
            direction = f"AI ranks {diff} spots higher"
        else:
            direction = f"AI ranks {-diff} spots lower"

        # Break total_score into component shares to find what's driving divergence
        total = abs(rec.z_score_value) + abs(rec.scarcity_bonus) + abs(rec.need_bonus)
        if total == 0:
            return direction

        drivers = []
        # Only flag components that are meaningful contributors
        if rec.scarcity_bonus > 0.3:
            scarce_pos = [p for p in rec.player.positions if p not in ("OF", "DH")]
            pos_label = f" ({scarce_pos[0]})" if scarce_pos else ""
            drivers.append(f"positional scarcity{pos_label}")
        if rec.need_bonus > 0.5:
            drivers.append("fills roster category needs")
        if rec.best_tier and rec.best_tier.tier <= 2 and rec.best_tier.remaining_in_tier <= 3:
            drivers.append(f"last {rec.best_tier.remaining_in_tier} in {rec.best_tier.position} Tier {rec.best_tier.tier}")

        if diff > 0 and not drivers:
            # AI likes them more than consensus — projections must be the reason
            drivers.append("projections outpace ADP")
        elif diff < 0 and not drivers:
            drivers.append("ADP outpaces projections")

        return f"{direction} — {', '.join(drivers)}"

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

        # Player tags
        if player.tags:
            tag_labels = {"rookie": "ROOKIE", "breakout": "BREAKOUT CANDIDATE", "sleeper": "SLEEPER"}
            parts.append(" | ".join(tag_labels.get(t, t.upper()) for t in player.tags))

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
            if cat == "AVG":
                total_ab = sum(
                    p.hitting_projections.get("AB", 0) for p in roster if p.is_hitter
                )
                if total_ab > 0:
                    total_h = sum(
                        p.hitting_projections.get("H", 0) for p in roster if p.is_hitter
                    )
                    totals[cat] = round(total_h / total_ab, 3)
                else:
                    totals[cat] = 0
            else:
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

    def category_dashboard(self, roster: list[Player]) -> dict:
        """Category projections with strength/weakness grades for strategy decisions.

        Returns projections, per-category grades, and a strategy hint.
        """
        projections = self.analyze_roster(roster)

        # League-winning benchmarks (12-team H2H, full-season)
        strong = {
            "HR": 250, "R": 850, "RBI": 830, "SB": 130, "AVG": 0.270,
            "QS": 115, "SV": 70, "K": 1300, "ERA": 3.50, "WHIP": 1.15,
        }
        weak = {
            "HR": 175, "R": 600, "RBI": 580, "SB": 90, "AVG": 0.255,
            "QS": 80, "SV": 30, "K": 900, "ERA": 4.20, "WHIP": 1.30,
        }

        grades = {}
        for cat in self.config.all_categories:
            val = projections.get(cat, 0)
            s, w = strong.get(cat, 0), weak.get(cat, 0)
            inverse = cat in self.config.inverse_categories

            if inverse:
                if val == 0:
                    grades[cat] = "-"
                elif val <= s:
                    grades[cat] = "strong"
                elif val >= w:
                    grades[cat] = "weak"
                else:
                    grades[cat] = "average"
            else:
                if val == 0:
                    grades[cat] = "-"
                elif val >= s:
                    grades[cat] = "strong"
                elif val <= w:
                    grades[cat] = "weak"
                else:
                    grades[cat] = "average"

        # Strategy hint
        strong_cats = [c for c, g in grades.items() if g == "strong"]
        weak_cats = [c for c, g in grades.items() if g == "weak"]

        hint = ""
        if len(strong_cats) >= 3:
            hint = f"Strong in {', '.join(strong_cats)}."
            if weak_cats:
                puntable = [c for c in weak_cats if c in ("SB", "SV", "AVG")]
                if puntable:
                    hint += f" Consider punting {'/'.join(puntable)}."
        elif weak_cats:
            hint = f"Gaps in {', '.join(weak_cats)} — address or commit to punting."

        return {
            "projections": projections,
            "grades": grades,
            "strategy_hint": hint,
        }

    @staticmethod
    def _prob_at_least_one_survives(
        viable_count: int, team_pick_rates: list[float]
    ) -> float:
        """Probability that at least one viable player survives all threat picks.

        Models each needing team as independently deciding to draft this position
        with their individual pick_rate. Uses dynamic programming to compute the
        probability distribution of total players taken.

        Args:
            viable_count: Number of viable players at this position
            team_pick_rates: Per-team probability of drafting this position
        """
        if not team_pick_rates:
            return 1.0

        # DP: prob[k] = probability that exactly k teams draft this position
        # Start with 0 teams having drafted
        prob = [0.0] * (len(team_pick_rates) + 1)
        prob[0] = 1.0

        for p_draft in team_pick_rates:
            new_prob = [0.0] * len(prob)
            for k in range(len(prob)):
                if prob[k] == 0:
                    continue
                # This team doesn't draft this position
                new_prob[k] += prob[k] * (1 - p_draft)
                # This team drafts this position
                if k + 1 < len(prob):
                    new_prob[k + 1] += prob[k] * p_draft
            prob = new_prob

        # P(at least one survives) = P(taken < viable_count)
        return sum(prob[k] for k in range(min(viable_count, len(prob))))

    def pick_safety(
        self,
        available: list[Player],
        my_roster: list[Player],
        threat_window: list[dict],
        pool: str | None = None,
        z_scores: dict[str, float] | None = None,
        tier_map: dict[str, list[TierInfo]] | None = None,
    ) -> list[PickSafety]:
        """Compute pick safety for each position.

        Uses projected availability (ADP-based probability) combined with
        opponent roster context from the threat window.

        Args:
            available: Available players (already filtered for pool)
            my_roster: Current roster players
            threat_window: From Draft.threat_window() — picks before user's next turn
                           with team positions_filled data
            pool: "hitter" or "pitcher"
            z_scores: Precomputed z-scores (optional, avoids recomputation if
                      recommend() was already called on the same available list)
            tier_map: Precomputed tier map (optional, same purpose)

        Returns list of PickSafety for relevant unfilled positions, sorted by urgency.
        """
        if pool == "hitter":
            positions = ["C", "1B", "2B", "SS", "3B", "LF", "CF", "RF"]
        elif pool == "pitcher":
            positions = ["SP", "RP"]
        else:
            positions = ["C", "1B", "2B", "SS", "3B", "LF", "CF", "RF", "SP", "RP"]

        # What positions has the user already filled?
        my_filled = set()
        for p in my_roster:
            if len(p.positions) == 1:
                my_filled.add(p.positions[0])
            elif pool == "hitter":
                hitter_pos = [pos for pos in p.positions if pos not in ("SP", "RP")]
                if len(hitter_pos) == 1:
                    my_filled.add(hitter_pos[0])

        # Filter to positions user still needs
        # For IF/OF flex slots, don't mark sub-positions as filled unless we have
        # more players at that position than roster slots
        positions_to_check = [pos for pos in positions if pos not in my_filled]
        if not positions_to_check:
            return []

        picks_before = len(threat_window)
        if picks_before == 0:
            return []

        # Use precomputed z-scores/tiers if provided (avoids duplicate work
        # when recommend() was already called on the same available list)
        if z_scores is None:
            z_scores = self._compute_z_scores(available, pool=pool)
        if tier_map is None:
            tier_map = self._compute_tiers(available, z_scores, pool=pool)

        all_positions = set(positions)

        results = []
        for pos in positions_to_check:
            # Find viable players at this position (in top 2 tiers)
            pos_players = []
            for p in available:
                if p.name not in z_scores:
                    continue
                eligible = pos in p.positions
                if not eligible and pos in ("LF", "CF", "RF") and "OF" in p.positions:
                    eligible = True
                if eligible:
                    player_tiers = tier_map.get(p.name, [])
                    pos_tier = next((t for t in player_tiers if t.position == pos), None)
                    tier_num = pos_tier.tier if pos_tier else 99
                    pos_players.append((p, tier_num))

            if not pos_players:
                continue

            # "Viable" = in top 2 tiers at this position
            viable = [p for p, t in pos_players if t <= 2]
            if not viable:
                # If no tier 1-2 players, use tier 3
                viable = [p for p, t in pos_players if t <= 3]
            if not viable:
                continue

            viable_count = len(viable)

            # Count teams picking before us that haven't filled this position,
            # and estimate each team's probability of drafting this position
            # based on how many unfilled positions they have.
            # Apply league ADP adjustment: if the league historically delays
            # a position, discount pick_rate proportionally.
            league_adj = self.config.league_adp_adjustments.get(pos, 0)
            teams_needing = 0
            team_pick_rates = []
            for tw in threat_window:
                if pos not in tw["positions_filled"]:
                    teams_needing += 1
                    # More unfilled positions = lower chance of picking THIS one
                    unfilled = len(all_positions) - len(tw["positions_filled"])
                    pick_rate = 1.0 / max(unfilled, 1)
                    # League tendency: discount by ~30% per round of delay
                    if league_adj > 0:
                        pick_rate *= max(0.05, 1 - 0.3 * league_adj)
                    team_pick_rates.append(pick_rate)

            # Probability at least one viable player survives.
            # Model: each needing team independently drafts this position with
            # their pick_rate. We want P(total_taken < viable_count).
            # Use sequential simulation: probability of survival through each team.
            if teams_needing == 0:
                prob_available = 1.0
            else:
                # P(at least one survives) = 1 - P(all viable get taken)
                # Compute P(k teams draft this position) and check if k < viable_count
                # Using binomial-like chain with varying per-team probabilities
                prob_available = self._prob_at_least_one_survives(
                    viable_count, team_pick_rates
                )

            # Determine signal
            if prob_available >= 0.70:
                signal = "safe"
            elif prob_available >= 0.35:
                signal = "monitor"
            else:
                signal = "reach"

            # Build detail string
            detail = (
                f"{viable_count} viable in tier, "
                f"{picks_before} picks before turn, "
                f"{teams_needing} teams still need {pos}"
            )
            if signal == "safe" and teams_needing == 0:
                detail += f" — no one ahead needs {pos}, safe to wait"
            elif signal == "reach":
                detail += f" — tier depleting fast, consider drafting now"

            results.append(PickSafety(
                position=pos,
                viable_in_tier=viable_count,
                picks_before_turn=picks_before,
                teams_needing=teams_needing,
                prob_available=round(prob_available, 2),
                signal=signal,
                detail=detail,
            ))

        # Sort by urgency: reach first, then monitor, then safe
        signal_order = {"reach": 0, "monitor": 1, "safe": 2}
        results.sort(key=lambda r: (signal_order[r.signal], r.prob_available))

        return results

    def _load_league_history(self, history_path: str) -> list[dict]:
        """Load and cache league history data. Returns empty list if missing."""
        if not hasattr(self, "_league_history_cache"):
            self._league_history_cache: dict[str, list[dict]] = {}
        if history_path not in self._league_history_cache:
            p = Path(history_path)
            if not p.exists():
                self._league_history_cache[history_path] = []
            else:
                with open(p, encoding="utf-8") as f:
                    data = json.load(f)
                self._league_history_cache[history_path] = data.get("teams", [])
        return self._league_history_cache[history_path]

    def league_relative_dashboard(
        self, my_roster: list[Player],
        available: list[Player] | None = None,
        history_path: str = "data/league_history.json",
    ) -> dict:
        """Category projections ranked against last season's actual league stats.

        Fills unfilled roster slots with next-best available players (by ADP)
        to project a realistic full-season roster, then ranks against
        historical league data.

        Returns scaled_projections, rankings (1-N), deltas (%), and hint.
        For inverse stats (ERA, WHIP), sign is flipped so + always = good.
        """
        empty_result = {
            "scaled_projections": {},
            "rankings": {},
            "deltas": {},
            "hint": "",
        }

        if not my_roster:
            return empty_result

        hist_teams = self._load_league_history(history_path)
        if not hist_teams:
            return empty_result

        target_hitters = sum(
            v for k, v in self.config.roster_slots.items()
            if k not in ("SP", "RP")
        )
        target_pitchers = sum(
            v for k, v in self.config.roster_slots.items()
            if k in ("SP", "RP")
        )

        # Build projected full roster by filling empty slots from available pool
        n_hitters = sum(1 for p in my_roster if p.is_hitter)
        n_pitchers = sum(1 for p in my_roster if p.is_pitcher)
        fill_hitters = max(0, target_hitters - n_hitters)
        fill_pitchers = max(0, target_pitchers - n_pitchers)

        projected_roster = list(my_roster)
        if available and (fill_hitters > 0 or fill_pitchers > 0):
            by_adp = sorted(available, key=lambda p: p.adp)
            rostered_ids = {p.player_id for p in my_roster}
            h_filled, p_filled = 0, 0
            for p in by_adp:
                if p.player_id in rostered_ids:
                    continue
                if h_filled >= fill_hitters and p_filled >= fill_pitchers:
                    break
                if p.is_hitter and h_filled < fill_hitters:
                    projected_roster.append(p)
                    h_filled += 1
                elif p.is_pitcher and p_filled < fill_pitchers:
                    projected_roster.append(p)
                    p_filled += 1

        scaled = self.analyze_roster(projected_roster)

        rankings: dict[str, int] = {}
        deltas: dict[str, float] = {}

        for cat in self.config.all_categories:
            my_val = scaled[cat]
            hist_vals = sorted(
                [t[cat] for t in hist_teams],
                reverse=(cat not in self.config.inverse_categories),
            )

            # Where would my scaled projection rank?
            rank = 1
            for hv in hist_vals:
                if cat in self.config.inverse_categories:
                    if my_val <= hv:
                        break
                else:
                    if my_val >= hv:
                        break
                rank += 1
            rankings[cat] = min(rank, len(hist_vals))

            # Delta vs league median
            n = len(hist_vals)
            if n >= 2:
                median = (hist_vals[n // 2 - 1] + hist_vals[n // 2]) / 2
            elif n == 1:
                median = hist_vals[0]
            else:
                median = 0
            if median != 0:
                pct = (my_val - median) / median * 100
                if cat in self.config.inverse_categories:
                    pct = -pct
                deltas[cat] = round(pct, 1)
            else:
                deltas[cat] = 0.0

        # Generate hint from rankings
        top3 = [cat for cat, r in rankings.items() if r <= 3]
        bottom3 = [cat for cat, r in rankings.items() if r >= 10]
        hint = ""
        if top3 and bottom3:
            puntable = [c for c in bottom3 if c in ("SB", "SV", "AVG")]
            if puntable:
                hint = f"Top 3 in {', '.join(top3)}. Bottom 3 in {', '.join(bottom3)} — consider punting {'/'.join(puntable)}."
            else:
                hint = f"Top 3 in {', '.join(top3)}. Bottom 3 in {', '.join(bottom3)} — address or accept."
        elif bottom3:
            hint = f"Bottom 3 in {', '.join(bottom3)} — address or commit to punting."
        elif top3:
            hint = f"Projected top 3 in {', '.join(top3)}."

        return {
            "scaled_projections": scaled,
            "rankings": rankings,
            "deltas": deltas,
            "hint": hint,
        }
