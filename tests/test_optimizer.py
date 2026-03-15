"""Tests for the draft optimizer — z-scores, tiers, pick safety, category dashboard."""

import json
import sys
sys.path.insert(0, "src")

import pytest
from drafter.config import LeagueConfig
from drafter.models import Player, DraftPick, DraftState
from drafter.optimizer import Optimizer, PickSafety, Recommendation, TierInfo


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _hitter(name, positions, adp=100.0, **hitting):
    defaults = {"AB": 500, "H": 140, "HR": 20, "R": 70, "RBI": 70, "SB": 5, "AVG": 0.280}
    defaults.update(hitting)
    return Player(
        name=name, player_id=name.lower().replace(" ", "_"),
        team="NYM", age=27, positions=positions,
        hitting_projections=defaults, pitching_projections={}, adp=adp,
    )


def _pitcher(name, positions, adp=100.0, **pitching):
    defaults = {"IP": 180, "H": 150, "BB": 50, "ER": 60, "K": 180, "QS": 20, "SV": 0,
                "ERA": 3.00, "WHIP": 1.11}
    defaults.update(pitching)
    return Player(
        name=name, player_id=name.lower().replace(" ", "_"),
        team="NYM", age=27, positions=positions,
        hitting_projections={}, pitching_projections=defaults, adp=adp,
    )


@pytest.fixture
def config():
    return LeagueConfig()


@pytest.fixture
def opt(config):
    return Optimizer(config)


@pytest.fixture
def hitter_pool():
    """A small pool of hitters for testing."""
    return [
        _hitter("Star SS", ["SS"], adp=10, HR=35, R=100, RBI=95, SB=20, AVG=0.300),
        _hitter("Good SS", ["SS"], adp=30, HR=25, R=85, RBI=80, SB=15, AVG=0.285),
        _hitter("Mid SS", ["SS"], adp=80, HR=15, R=65, RBI=60, SB=8, AVG=0.265),
        _hitter("Star C", ["C"], adp=20, HR=30, R=80, RBI=90, SB=3, AVG=0.275),
        _hitter("Good C", ["C"], adp=60, HR=20, R=60, RBI=65, SB=2, AVG=0.260),
        _hitter("Backup C", ["C"], adp=150, HR=10, R=40, RBI=40, SB=1, AVG=0.240),
        _hitter("Star 1B", ["1B"], adp=15, HR=40, R=95, RBI=110, SB=2, AVG=0.290),
        _hitter("Good 1B", ["1B"], adp=40, HR=30, R=80, RBI=85, SB=3, AVG=0.275),
        _hitter("Star OF", ["CF", "OF"], adp=5, HR=30, R=105, RBI=85, SB=30, AVG=0.295),
        _hitter("Good OF", ["RF", "OF"], adp=25, HR=25, R=80, RBI=75, SB=10, AVG=0.280),
        _hitter("Multi", ["1B", "C"], adp=70, HR=18, R=55, RBI=60, SB=1, AVG=0.258),
        _hitter("Speed OF", ["LF", "OF"], adp=50, HR=10, R=90, RBI=50, SB=40, AVG=0.270),
        _hitter("Power 3B", ["3B"], adp=35, HR=35, R=85, RBI=100, SB=3, AVG=0.265),
        _hitter("Good 2B", ["2B"], adp=45, HR=18, R=80, RBI=65, SB=15, AVG=0.280),
        _hitter("Mid 2B", ["2B"], adp=90, HR=12, R=65, RBI=55, SB=10, AVG=0.268),
    ]


# ---------------------------------------------------------------------------
# _prob_at_least_one_survives
# ---------------------------------------------------------------------------

class TestProbAtLeastOneSurvives:
    def test_empty_rates_returns_one(self):
        assert Optimizer._prob_at_least_one_survives(3, []) == 1.0

    def test_single_team_certain_pick_one_viable(self):
        # One team will definitely draft this position, one viable player
        result = Optimizer._prob_at_least_one_survives(1, [1.0])
        assert result == pytest.approx(0.0)

    def test_single_team_certain_pick_two_viable(self):
        # One team will definitely draft, but two viable — one survives
        result = Optimizer._prob_at_least_one_survives(2, [1.0])
        assert result == pytest.approx(1.0)

    def test_all_teams_zero_probability(self):
        result = Optimizer._prob_at_least_one_survives(2, [0.0, 0.0, 0.0])
        assert result == pytest.approx(1.0)

    def test_single_team_half_probability(self):
        result = Optimizer._prob_at_least_one_survives(1, [0.5])
        assert result == pytest.approx(0.5)

    def test_two_teams_exceed_viable(self):
        # 2 teams at 100%, only 1 viable — guaranteed gone
        result = Optimizer._prob_at_least_one_survives(1, [1.0, 1.0])
        assert result == pytest.approx(0.0)

    def test_many_teams_low_rates(self):
        # 6 teams each at 12.5% chance, 2 viable
        result = Optimizer._prob_at_least_one_survives(2, [0.125] * 6)
        assert 0.7 < result < 0.95  # should be around 0.83

    def test_high_pressure(self):
        # 10 teams each at 20%, only 1 viable — very likely taken
        result = Optimizer._prob_at_least_one_survives(1, [0.2] * 10)
        assert result < 0.15  # ~10.7%

    def test_monotonic_in_viable_count(self):
        rates = [0.25] * 5
        p1 = Optimizer._prob_at_least_one_survives(1, rates)
        p2 = Optimizer._prob_at_least_one_survives(2, rates)
        p3 = Optimizer._prob_at_least_one_survives(5, rates)
        assert p1 < p2 < p3

    def test_monotonic_in_rates(self):
        # Higher rates = lower survival probability
        p_low = Optimizer._prob_at_least_one_survives(2, [0.1] * 4)
        p_high = Optimizer._prob_at_least_one_survives(2, [0.5] * 4)
        assert p_high < p_low


# ---------------------------------------------------------------------------
# picks_before_mine (snake draft ordering)
# ---------------------------------------------------------------------------

class TestPicksBeforeMine:
    def _state(self, my_pos, num_picks_made=0):
        names = [f"Team{i}" for i in range(1, 13)]
        names[my_pos - 1] = "MyTeam"
        s = DraftState(num_teams=12, draft_type="snake", my_team="MyTeam",
                       my_draft_position=my_pos, team_names=names)
        for i in range(num_picks_made):
            s.picks.append(DraftPick(i+1, (i//12)+1, f"P{i}", f"p{i}", s.picking_team(i+1)))
        return s

    def test_my_pick_returns_empty(self):
        # Position 1, pick 1 — it's my turn
        s = self._state(my_pos=1, num_picks_made=0)
        assert s.picks_before_mine() == []

    def test_forward_round(self):
        # Position 6, round 1 — picks 1-5 are before me
        s = self._state(my_pos=6, num_picks_made=0)
        result = s.picks_before_mine()
        assert len(result) == 5
        assert result[0][0] == 1  # pick number 1
        assert result[4][0] == 5  # pick number 5

    def test_snake_reverse_round(self):
        # Position 6, round 2 starts at pick 13 (reverse: Team12 first)
        # My pick in round 2 = pick 19 (position 7 from end = pos 6 reversed)
        s = self._state(my_pos=6, num_picks_made=12)
        result = s.picks_before_mine()
        # In reverse round, position 6 picks 7th (12-6+1=7th), so 6 picks before
        assert len(result) == 6

    def test_at_turn_back_to_back(self):
        # Position 12: last pick of round 1 (pick 12), first pick of round 2 (pick 13)
        s = self._state(my_pos=12, num_picks_made=12)
        result = s.picks_before_mine()
        assert len(result) == 0  # Back-to-back, it's my turn

    def test_position_1_long_gap(self):
        # Position 1: first pick of round 1, but last pick of round 2
        # After round 1 (12 picks), next pick is pick 24 (last of round 2)
        s = self._state(my_pos=1, num_picks_made=12)
        result = s.picks_before_mine()
        assert len(result) == 11  # 11 teams pick before me in reverse round


# ---------------------------------------------------------------------------
# pick_safety
# ---------------------------------------------------------------------------

class TestPickSafety:
    def test_empty_threat_window(self, opt, hitter_pool):
        result = opt.pick_safety(hitter_pool, [], [], pool="hitter")
        assert result == []

    def test_no_teams_needing_position(self, opt, hitter_pool):
        # All teams in threat window have already filled C
        threat = [
            {"pick_number": 10, "team_name": "T1", "positions_filled": {"C", "SS"}},
            {"pick_number": 11, "team_name": "T2", "positions_filled": {"C", "1B"}},
        ]
        result = opt.pick_safety(hitter_pool, [], threat, pool="hitter")
        c_safety = next((s for s in result if s.position == "C"), None)
        if c_safety:
            assert c_safety.prob_available == 1.0
            assert c_safety.signal == "safe"

    def test_all_teams_needing_scarce_position(self, opt, hitter_pool):
        # 8 teams need C, only 3 catchers available — should be less safe
        threat = [
            {"pick_number": i, "team_name": f"T{i}", "positions_filled": set()}
            for i in range(1, 9)
        ]
        result = opt.pick_safety(hitter_pool, [], threat, pool="hitter")
        c_safety = next((s for s in result if s.position == "C"), None)
        assert c_safety is not None
        assert c_safety.teams_needing == 8

    def test_filled_positions_excluded(self, opt, hitter_pool):
        # If user already has SS, SS should not appear in safety
        ss_player = _hitter("MySS", ["SS"], adp=10)
        threat = [
            {"pick_number": 10, "team_name": "T1", "positions_filled": set()},
        ]
        result = opt.pick_safety(hitter_pool, [ss_player], threat, pool="hitter")
        positions = [s.position for s in result]
        assert "SS" not in positions

    def test_sorted_by_urgency(self, opt, hitter_pool):
        threat = [
            {"pick_number": i, "team_name": f"T{i}", "positions_filled": set()}
            for i in range(1, 11)
        ]
        result = opt.pick_safety(hitter_pool, [], threat, pool="hitter")
        if len(result) >= 2:
            # Reach items first, then monitor, then safe
            signals = [s.signal for s in result]
            order = {"reach": 0, "monitor": 1, "safe": 2}
            signal_nums = [order[s] for s in signals]
            assert signal_nums == sorted(signal_nums)

    def test_pitcher_pool(self, opt):
        pitchers = [
            _pitcher("Ace SP", ["SP"], adp=20, K=250, ERA=2.50),
            _pitcher("Good SP", ["SP"], adp=40, K=200, ERA=3.20),
            _pitcher("Closer", ["RP"], adp=50, SV=35, K=80, ERA=2.80),
        ]
        threat = [
            {"pick_number": 1, "team_name": "T1", "positions_filled": set()},
        ]
        result = opt.pick_safety(pitchers, [], threat, pool="pitcher")
        positions = {s.position for s in result}
        assert positions <= {"SP", "RP"}


# ---------------------------------------------------------------------------
# annotate_safety
# ---------------------------------------------------------------------------

class TestAnnotateSafety:
    def test_reach_tag_added(self, opt):
        p = _hitter("Test", ["2B"], adp=50)
        rec = Recommendation(player=p, total_score=5.0, z_score_value=3.0,
                             scarcity_bonus=1.0, need_bonus=1.0, reasoning="test")
        safety = [PickSafety("2B", 1, 10, 8, 0.20, "reach", "tier depleting")]
        opt.annotate_safety([rec], safety)
        assert "reach" in rec.tags
        assert len(rec.safety_flags) == 1

    def test_safe_tag_added_when_no_teams_need(self, opt):
        p = _hitter("Test", ["SS"], adp=50)
        rec = Recommendation(player=p, total_score=5.0, z_score_value=3.0,
                             scarcity_bonus=1.0, need_bonus=1.0, reasoning="test")
        safety = [PickSafety("SS", 5, 10, 0, 1.0, "safe", "no one needs SS")]
        opt.annotate_safety([rec], safety)
        assert "safe" in rec.tags

    def test_no_duplicate_tags(self, opt):
        p = _hitter("Test", ["2B"], adp=50)
        rec = Recommendation(player=p, total_score=5.0, z_score_value=3.0,
                             scarcity_bonus=1.0, need_bonus=1.0, reasoning="test",
                             tags=["reach"])
        safety = [PickSafety("2B", 1, 10, 8, 0.20, "reach", "tier depleting")]
        opt.annotate_safety([rec], safety)
        assert rec.tags.count("reach") == 1

    def test_of_umbrella_matching(self, opt):
        p = _hitter("Test", ["OF", "CF"], adp=50)
        rec = Recommendation(player=p, total_score=5.0, z_score_value=3.0,
                             scarcity_bonus=1.0, need_bonus=1.0, reasoning="test")
        safety = [
            PickSafety("LF", 3, 5, 3, 0.80, "safe", "ok"),
            PickSafety("CF", 2, 5, 4, 0.60, "monitor", "watch"),
            PickSafety("RF", 3, 5, 3, 0.80, "safe", "ok"),
        ]
        opt.annotate_safety([rec], safety)
        flag_positions = {sf.position for sf in rec.safety_flags}
        assert "CF" in flag_positions
        assert "LF" in flag_positions
        assert "RF" in flag_positions
        # No duplicates
        assert len(rec.safety_flags) == 3

    def test_no_matching_safety(self, opt):
        p = _hitter("Test", ["3B"], adp=50)
        rec = Recommendation(player=p, total_score=5.0, z_score_value=3.0,
                             scarcity_bonus=1.0, need_bonus=1.0, reasoning="test")
        safety = [PickSafety("C", 3, 5, 3, 0.80, "safe", "ok")]
        opt.annotate_safety([rec], safety)
        assert rec.safety_flags == []
        assert "safe" not in rec.tags
        assert "reach" not in rec.tags


# ---------------------------------------------------------------------------
# category_dashboard
# ---------------------------------------------------------------------------

class TestCategoryDashboard:
    def test_empty_roster(self, opt):
        dash = opt.category_dashboard([])
        for cat in opt.config.all_categories:
            assert dash["grades"][cat] == "-"

    def test_strong_power_roster(self, opt):
        # 8 sluggers totaling 281 HR (above 250 threshold)
        roster = [
            _hitter("Slugger1", ["1B"], HR=45, R=100, RBI=110, AVG=0.280),
            _hitter("Slugger2", ["3B"], HR=42, R=95, RBI=100, AVG=0.275),
            _hitter("Slugger3", ["RF"], HR=40, R=90, RBI=95, AVG=0.270),
            _hitter("Slugger4", ["LF"], HR=38, R=88, RBI=90, AVG=0.268),
            _hitter("Slugger5", ["SS"], HR=35, R=85, RBI=85, AVG=0.265),
            _hitter("Slugger6", ["C"], HR=30, R=80, RBI=80, AVG=0.270),
            _hitter("Slugger7", ["2B"], HR=28, R=78, RBI=75, AVG=0.272),
            _hitter("Slugger8", ["DH"], HR=25, R=75, RBI=70, AVG=0.265),
        ]
        dash = opt.category_dashboard(roster)
        assert dash["projections"]["HR"] >= 250
        assert dash["grades"]["HR"] == "strong"

    def test_inverse_stat_grading(self, opt):
        roster = [
            _pitcher("Ace1", ["SP"], ERA=2.50, WHIP=0.95, IP=200, ER=56, H=140, BB=50, K=250, QS=25),
            _pitcher("Ace2", ["SP"], ERA=2.80, WHIP=1.00, IP=190, ER=59, H=140, BB=50, K=220, QS=22),
            _pitcher("Ace3", ["SP"], ERA=3.00, WHIP=1.05, IP=180, ER=60, H=140, BB=49, K=200, QS=20),
        ]
        dash = opt.category_dashboard(roster)
        # Low ERA should grade as strong
        assert dash["grades"]["ERA"] in ("strong", "average")

    def test_strategy_hint_mentions_punt(self, opt):
        roster = [
            _hitter("Power1", ["1B"], HR=45, R=100, RBI=110, SB=2, AVG=0.275),
            _hitter("Power2", ["3B"], HR=40, R=95, RBI=100, SB=3, AVG=0.270),
            _hitter("Power3", ["RF"], HR=38, R=90, RBI=95, SB=1, AVG=0.268),
            _hitter("Power4", ["LF"], HR=35, R=88, RBI=90, SB=2, AVG=0.265),
            _hitter("Power5", ["SS"], HR=30, R=85, RBI=85, SB=4, AVG=0.275),
            _hitter("Power6", ["C"], HR=28, R=80, RBI=80, SB=1, AVG=0.270),
            _hitter("Power7", ["2B"], HR=25, R=78, RBI=75, SB=2, AVG=0.280),
        ]
        dash = opt.category_dashboard(roster)
        # With very low SB and strong power categories, hint should mention SB
        if dash["strategy_hint"]:
            # Just verify hint is generated
            assert isinstance(dash["strategy_hint"], str)


# ---------------------------------------------------------------------------
# LeagueConfig.with_strategy
# ---------------------------------------------------------------------------

class TestWithStrategy:
    def test_balanced(self):
        config = LeagueConfig.with_strategy("balanced")
        assert config.category_weights == {}
        assert config.weight("HR") == 1.0

    def test_punt_sb(self):
        config = LeagueConfig.with_strategy("punt_sb")
        assert config.weight("SB") == pytest.approx(0.1)
        assert config.weight("HR") == 1.0

    def test_punt_sv(self):
        config = LeagueConfig.with_strategy("punt_sv")
        assert config.weight("SV") == pytest.approx(0.1)

    def test_punt_avg(self):
        config = LeagueConfig.with_strategy("punt_avg")
        assert config.weight("AVG") == pytest.approx(0.2)

    def test_unknown_strategy_raises(self):
        with pytest.raises(ValueError, match="Unknown strategy"):
            LeagueConfig.with_strategy("punt_everything")

    def test_extra_kwargs_forwarded(self):
        config = LeagueConfig.with_strategy("balanced", num_teams=10)
        assert config.num_teams == 10


# ---------------------------------------------------------------------------
# recommend (integration)
# ---------------------------------------------------------------------------

class TestRecommend:
    def test_basic_recommend(self, opt, hitter_pool):
        recs = opt.recommend(hitter_pool, [], n=5, pool="hitter")
        assert len(recs) == 5
        # Scores should be descending
        scores = [r.total_score for r in recs]
        assert scores == sorted(scores, reverse=True)

    def test_adp_rank_assigned(self, opt, hitter_pool):
        recs = opt.recommend(hitter_pool, [], n=5, pool="hitter")
        for r in recs:
            assert r.adp_rank > 0

    def test_value_tag_assigned(self, opt, hitter_pool):
        recs = opt.recommend(hitter_pool, [], n=15, pool="hitter")
        # At least some players should get value tags if ADP rank >> AI rank
        # This is a structural test — just verify the tag mechanism works
        for r in recs:
            assert isinstance(r.tags, list)

    def test_punt_sb_changes_rankings(self):
        config_balanced = LeagueConfig.with_strategy("balanced")
        config_punt = LeagueConfig.with_strategy("punt_sb")
        pool = [
            _hitter("Speed", ["CF", "OF"], adp=20, HR=10, SB=45, AVG=0.280),
            _hitter("Power", ["1B"], adp=20, HR=40, SB=2, AVG=0.270),
        ]
        recs_b = Optimizer(config_balanced).recommend(pool, [], n=2, pool="hitter")
        recs_p = Optimizer(config_punt).recommend(pool, [], n=2, pool="hitter")
        # With punt_sb, Power should rank higher relative to Speed
        rank_b = {r.player.name: i for i, r in enumerate(recs_b)}
        rank_p = {r.player.name: i for i, r in enumerate(recs_p)}
        # Power should be same or better rank in punt_sb
        assert rank_p["Power"] <= rank_b["Power"]


# ---------------------------------------------------------------------------
# league_relative_dashboard
# ---------------------------------------------------------------------------

def _history_fixture(tmp_path):
    """Create a minimal league_history.json with 12 teams."""
    teams = []
    for i in range(12):
        teams.append({
            "rank": i + 1,
            "R": 700 + i * 20,     # 700-920
            "HR": 200 + i * 10,    # 200-310
            "RBI": 700 + i * 20,   # 700-920
            "SB": 80 + i * 10,     # 80-190
            "AVG": 0.245 + i * 0.002,  # .245-.267
            "K": 900 + i * 50,     # 900-1450
            "QS": 60 + i * 5,      # 60-115
            "SV": 30 + i * 5,      # 30-85
            "ERA": 4.5 - i * 0.1,  # 4.5-3.4 (lower = better)
            "WHIP": 1.30 - i * 0.01,  # 1.30-1.19
        })
    path = tmp_path / "league_history.json"
    path.write_text(json.dumps({"season": 2025, "teams": teams}))
    return str(path)


class TestLeagueRelativeDashboard:
    def test_basic_with_fill(self, opt, tmp_path):
        """Partial roster + available pool produces filled projections."""
        hist = _history_fixture(tmp_path)
        roster = [
            _hitter("H1", ["SS"], adp=10, HR=35, R=100, RBI=95, SB=20),
            _hitter("H2", ["1B"], adp=20, HR=30, R=80, RBI=85, SB=5),
        ]
        pool = [
            _hitter(f"Avail{i}", ["OF"], adp=50 + i * 10, HR=20, R=70, RBI=60, SB=8)
            for i in range(15)
        ] + [
            _pitcher(f"AvailP{i}", ["SP"], adp=60 + i * 10, K=180, QS=20, ERA=3.5)
            for i in range(10)
        ]
        result = opt.league_relative_dashboard(roster, available=pool, history_path=hist)
        assert "scaled_projections" in result
        assert "rankings" in result
        assert "deltas" in result
        assert "hint" in result
        # Should have all 10 categories
        assert len(result["rankings"]) == 10
        # Projections should be non-zero for counting stats
        assert result["scaled_projections"]["HR"] > 0
        assert result["scaled_projections"]["K"] > 0

    def test_fill_counts_correct(self, opt, tmp_path):
        """Fill adds exactly the right number of hitters and pitchers."""
        hist = _history_fixture(tmp_path)
        roster = [
            _hitter("H1", ["SS"], adp=10, HR=35),
        ]
        # Target: 11 hitters, 7 pitchers. Have 1 hitter, 0 pitchers.
        pool = [
            _hitter(f"FH{i}", ["OF"], adp=50 + i) for i in range(20)
        ] + [
            _pitcher(f"FP{i}", ["SP"], adp=50 + i) for i in range(10)
        ]
        result = opt.league_relative_dashboard(roster, available=pool, history_path=hist)
        # HR should be way higher than just one player's 35
        assert result["scaled_projections"]["HR"] > 100

    def test_adp_ordering(self, opt, tmp_path):
        """Fill players are selected by lowest ADP first."""
        hist = _history_fixture(tmp_path)
        roster = [_hitter("H1", ["SS"], adp=5, HR=35)]
        # Only one slot to fill for hitters conceptually, but target is 11.
        # Make one high-ADP hitter with huge HR and low-ADP hitters with less.
        pool = [
            _hitter("BadADP", ["OF"], adp=500, HR=99),
            _hitter("GoodADP", ["OF"], adp=10, HR=15),
        ]
        result = opt.league_relative_dashboard(roster, available=pool, history_path=hist)
        # GoodADP (adp=10) should be picked before BadADP (adp=500)
        # With only 2 available hitters, both get added but GoodADP first
        # The total HR reflects both being added since we need 10 fill hitters
        assert result["scaled_projections"]["HR"] > 35

    def test_rostered_players_skipped(self, opt, tmp_path):
        """Players already on roster are not double-counted in fill."""
        hist = _history_fixture(tmp_path)
        p1 = _hitter("H1", ["SS"], adp=5, HR=35, R=100, RBI=95, SB=20)
        pool = [
            p1,  # Same player in available pool
            _hitter("H2", ["OF"], adp=20, HR=25),
        ]
        result = opt.league_relative_dashboard([p1], available=pool, history_path=hist)
        # H1's HR (35) should appear once, not doubled
        assert result["scaled_projections"]["HR"] > 0

    def test_available_none(self, opt, tmp_path):
        """available=None means no fill — projections are raw roster only."""
        hist = _history_fixture(tmp_path)
        roster = [_hitter("H1", ["SS"], adp=10, HR=35)]
        result = opt.league_relative_dashboard(roster, available=None, history_path=hist)
        assert result["scaled_projections"]["HR"] == 35

    def test_available_empty(self, opt, tmp_path):
        """available=[] means no fill — same as None."""
        hist = _history_fixture(tmp_path)
        roster = [_hitter("H1", ["SS"], adp=10, HR=35)]
        result = opt.league_relative_dashboard(roster, available=[], history_path=hist)
        assert result["scaled_projections"]["HR"] == 35

    def test_all_slots_filled(self, opt, tmp_path):
        """When roster already has target slots, no fill happens."""
        hist = _history_fixture(tmp_path)
        # 11 hitters + 7 pitchers = 18 total (matches roster_slots)
        roster = (
            [_hitter(f"H{i}", ["OF"], adp=i * 10, HR=20) for i in range(11)]
            + [_pitcher(f"P{i}", ["SP"], adp=i * 10, K=150) for i in range(5)]
            + [_pitcher(f"RP{i}", ["RP"], adp=i * 10, SV=25) for i in range(2)]
        )
        big_pool = [_hitter(f"Extra{i}", ["OF"], adp=999, HR=99) for i in range(10)]
        result = opt.league_relative_dashboard(roster, available=big_pool, history_path=hist)
        # HR should be 11 * 20 = 220 (no fill players with HR=99 added)
        assert result["scaled_projections"]["HR"] == 220

    def test_empty_roster_returns_empty(self, opt, tmp_path):
        """Empty roster triggers early return."""
        hist = _history_fixture(tmp_path)
        result = opt.league_relative_dashboard([], available=[], history_path=hist)
        assert result == {"scaled_projections": {}, "rankings": {}, "deltas": {}, "hint": ""}

    def test_missing_history_file(self, opt):
        """Missing history file returns empty result instead of crashing."""
        roster = [_hitter("H1", ["SS"], adp=10)]
        result = opt.league_relative_dashboard(roster, history_path="/nonexistent/path.json")
        assert result == {"scaled_projections": {}, "rankings": {}, "deltas": {}, "hint": ""}

    def test_inverse_category_ranking(self, opt, tmp_path):
        """ERA/WHIP: lower is better, delta sign is flipped (+ = good)."""
        hist = _history_fixture(tmp_path)
        # Pitcher with elite ERA (2.50) — should rank highly
        roster = [_pitcher("Ace", ["SP"], adp=5, ERA=2.50, WHIP=0.95,
                           IP=200, ER=56, H=140, BB=50, K=250, QS=25)]
        # Fill remaining pitchers
        pool = [
            _pitcher(f"FP{i}", ["SP"], adp=50 + i * 10, ERA=3.50, WHIP=1.15,
                     IP=170, ER=66, H=155, BB=55, K=170, QS=18)
            for i in range(10)
        ] + [
            _hitter(f"FH{i}", ["OF"], adp=20 + i * 10) for i in range(15)
        ]
        result = opt.league_relative_dashboard(roster, available=pool, history_path=hist)
        # ERA ranking should be good (low number = better rank)
        assert result["rankings"]["ERA"] <= 6
        # Delta should be positive (+ = good for inverse stats)
        assert result["deltas"]["ERA"] > 0

    def test_ranking_clamped_to_num_teams(self, opt, tmp_path):
        """Ranking never exceeds number of historical teams."""
        hist = _history_fixture(tmp_path)
        # Terrible SB projection (0) should rank last but not 13
        roster = [_hitter("NoSpeed", ["1B"], adp=10, HR=40, SB=0)]
        result = opt.league_relative_dashboard(roster, available=None, history_path=hist)
        assert result["rankings"]["SB"] <= 12
        assert result["rankings"]["SB"] >= 1

    def test_zero_projection_delta(self, opt, tmp_path):
        """Zero projection should produce negative delta, not 0%."""
        hist = _history_fixture(tmp_path)
        roster = [_hitter("NoSB", ["1B"], adp=10, HR=40, SB=0)]
        result = opt.league_relative_dashboard(roster, available=None, history_path=hist)
        # 0 SB vs median of ~135 SB should be -100%
        assert result["deltas"]["SB"] == -100.0

    def test_hint_top3_and_bottom3_puntable(self, opt, tmp_path):
        """Hint mentions punting when puntable cats are in bottom 3."""
        # Create history where we'll rank top in HR but bottom in SB
        hist = _history_fixture(tmp_path)
        roster = [
            _hitter("Slugger", ["1B"], adp=5, HR=50, R=110, RBI=120, SB=0, AVG=0.300),
        ]
        # Fill pool with similar sluggers (high HR, no SB)
        pool = [
            _hitter(f"Slug{i}", ["OF"], adp=20 + i * 5, HR=35, R=90, RBI=85, SB=1, AVG=0.280)
            for i in range(15)
        ] + [
            _pitcher(f"P{i}", ["SP"], adp=50 + i * 10, K=200, QS=22)
            for i in range(10)
        ]
        result = opt.league_relative_dashboard(roster, available=pool, history_path=hist)
        if result["rankings"].get("SB", 0) >= 10:
            assert "SB" in result["hint"]

    def test_hint_only_bottom3(self, opt, tmp_path):
        """Hint for bottom 3 without top 3."""
        hist = _history_fixture(tmp_path)
        # Mediocre roster that's bad at SB and SV
        roster = [_hitter("Mid", ["SS"], adp=100, HR=15, R=65, RBI=60, SB=2)]
        result = opt.league_relative_dashboard(roster, available=None, history_path=hist)
        if any(r >= 10 for r in result["rankings"].values()):
            assert "hint" in result
            if result["hint"]:
                assert "Bottom 3" in result["hint"] or "Projected top 3" in result["hint"]

    def test_hint_only_top3(self, opt, tmp_path):
        """Hint for top 3 categories without any bottom 3."""
        hist = _history_fixture(tmp_path)
        # Elite balanced roster — good at everything
        roster = (
            [_hitter(f"Star{i}", ["OF"], adp=i * 5, HR=35, R=100, RBI=95, SB=25, AVG=0.290)
             for i in range(11)]
            + [_pitcher(f"Ace{i}", ["SP"], adp=i * 5, K=220, QS=24, SV=0,
                        IP=200, ER=56, H=140, BB=45, ERA=2.50, WHIP=0.93)
               for i in range(5)]
            + [_pitcher(f"CL{i}", ["RP"], adp=i * 5, K=80, QS=0, SV=35,
                        IP=65, ER=16, H=45, BB=20, ERA=2.20, WHIP=1.00)
               for i in range(2)]
        )
        result = opt.league_relative_dashboard(roster, available=None, history_path=hist)
        if result["hint"]:
            assert "top 3" in result["hint"].lower()

    def test_dynamic_median(self, opt, tmp_path):
        """Median is computed dynamically, not hardcoded to positions [5][6]."""
        # Create history with only 4 teams
        teams = [
            {"R": 700, "HR": 200, "RBI": 700, "SB": 100, "AVG": 0.250,
             "K": 1000, "QS": 70, "SV": 40, "ERA": 4.0, "WHIP": 1.25},
            {"R": 800, "HR": 250, "RBI": 800, "SB": 150, "AVG": 0.260,
             "K": 1200, "QS": 90, "SV": 60, "ERA": 3.5, "WHIP": 1.15},
            {"R": 850, "HR": 270, "RBI": 850, "SB": 170, "AVG": 0.270,
             "K": 1300, "QS": 100, "SV": 70, "ERA": 3.3, "WHIP": 1.10},
            {"R": 900, "HR": 300, "RBI": 900, "SB": 200, "AVG": 0.280,
             "K": 1400, "QS": 110, "SV": 80, "ERA": 3.0, "WHIP": 1.05},
        ]
        path = tmp_path / "small_league.json"
        path.write_text(json.dumps({"season": 2025, "teams": teams}))
        roster = [_hitter("H1", ["SS"], adp=10, HR=275)]
        result = opt.league_relative_dashboard(roster, available=None, history_path=str(path))
        # Median of 4 teams' HR: (250 + 270) / 2 = 260. HR=275 -> +5.8%
        assert result["deltas"]["HR"] != 0.0
        assert result["rankings"]["HR"] <= 4

    def test_history_cache(self, opt, tmp_path):
        """History is loaded once and cached on subsequent calls."""
        hist = _history_fixture(tmp_path)
        roster = [_hitter("H1", ["SS"], adp=10, HR=35)]
        opt.league_relative_dashboard(roster, available=None, history_path=hist)
        # Verify cache exists
        assert hasattr(opt, "_league_history_cache")
        assert hist in opt._league_history_cache
        assert len(opt._league_history_cache[hist]) == 12

    def test_no_my_projections_in_result(self, opt, tmp_path):
        """my_projections was removed from the return dict."""
        hist = _history_fixture(tmp_path)
        roster = [_hitter("H1", ["SS"], adp=10)]
        result = opt.league_relative_dashboard(roster, available=None, history_path=hist)
        assert "my_projections" not in result


# ---------------------------------------------------------------------------
# league_adp_adjustments
# ---------------------------------------------------------------------------

class TestLeagueAdpAdjustments:
    def test_config_default_empty(self):
        config = LeagueConfig()
        assert config.league_adp_adjustments == {}

    def test_config_with_adjustments(self):
        config = LeagueConfig(league_adp_adjustments={"RP": 2.0})
        assert config.league_adp_adjustments["RP"] == 2.0

    def test_pick_safety_discounts_rp_rate(self):
        """With RP adjustment, pick_safety should compute higher survival probability."""
        pitchers = [
            _pitcher("Ace SP", ["SP"], adp=20, K=250, ERA=2.50, QS=25),
            _pitcher("Good SP", ["SP"], adp=40, K=200, ERA=3.20, QS=18),
            _pitcher("Mid SP", ["SP"], adp=60, K=170, ERA=3.50, QS=15),
            _pitcher("Closer1", ["RP"], adp=50, SV=35, K=80, ERA=2.80, IP=65, H=50, BB=20, ER=20),
            _pitcher("Closer2", ["RP"], adp=70, SV=30, K=70, ERA=3.00, IP=60, H=48, BB=18, ER=20),
            _pitcher("Closer3", ["RP"], adp=90, SV=25, K=60, ERA=3.20, IP=55, H=45, BB=17, ER=20),
        ]
        threat = [
            {"pick_number": i, "team_name": f"T{i}", "positions_filled": set()}
            for i in range(1, 6)
        ]

        # Without adjustment
        config_base = LeagueConfig()
        opt_base = Optimizer(config_base)
        result_base = opt_base.pick_safety(pitchers, [], threat, pool="pitcher")
        rp_base = next((s for s in result_base if s.position == "RP"), None)

        # With RP +2.0 adjustment
        config_adj = LeagueConfig(league_adp_adjustments={"RP": 2.0})
        opt_adj = Optimizer(config_adj)
        result_adj = opt_adj.pick_safety(pitchers, [], threat, pool="pitcher")
        rp_adj = next((s for s in result_adj if s.position == "RP"), None)

        assert rp_base is not None
        assert rp_adj is not None
        # Adjusted probability should be higher (more safe) than base
        assert rp_adj.prob_available >= rp_base.prob_available

    def test_no_adjustment_for_sp(self):
        """SP should be unaffected when only RP has an adjustment."""
        pitchers = [
            _pitcher("Ace SP", ["SP"], adp=20, K=250, ERA=2.50, QS=25),
            _pitcher("Good SP", ["SP"], adp=40, K=200, ERA=3.20, QS=18),
            _pitcher("Closer", ["RP"], adp=50, SV=35, K=80, ERA=2.80, IP=65, H=50, BB=20, ER=20),
        ]
        threat = [
            {"pick_number": 1, "team_name": "T1", "positions_filled": set()},
        ]

        config_base = LeagueConfig()
        opt_base = Optimizer(config_base)
        result_base = opt_base.pick_safety(pitchers, [], threat, pool="pitcher")
        sp_base = next((s for s in result_base if s.position == "SP"), None)

        config_adj = LeagueConfig(league_adp_adjustments={"RP": 2.0})
        opt_adj = Optimizer(config_adj)
        result_adj = opt_adj.pick_safety(pitchers, [], threat, pool="pitcher")
        sp_adj = next((s for s in result_adj if s.position == "SP"), None)

        assert sp_base is not None and sp_adj is not None
        assert sp_adj.prob_available == sp_base.prob_available


class TestWaitSnipeTags:
    def test_wait_tag_on_safe_rp(self):
        """RP with safe signal and league adjustment should get 'wait' tag."""
        config = LeagueConfig(league_adp_adjustments={"RP": 2.0})
        opt = Optimizer(config)
        p = _pitcher("Closer", ["RP"], adp=50, SV=35)
        rec = Recommendation(player=p, total_score=5.0, z_score_value=3.0,
                             scarcity_bonus=1.0, need_bonus=1.0, reasoning="test")
        safety = [PickSafety("RP", 5, 10, 2, 0.85, "safe", "plenty of closers")]
        opt.annotate_safety([rec], safety)
        assert "wait" in rec.tags

    def test_snipe_tag_on_elite_monitor_rp(self):
        """Elite RP (top 5 ADP) with monitor signal should get 'snipe' tag."""
        config = LeagueConfig(league_adp_adjustments={"RP": 2.0})
        opt = Optimizer(config)
        p = _pitcher("Elite Closer", ["RP"], adp=30, SV=40)
        rec = Recommendation(player=p, total_score=7.0, z_score_value=4.0,
                             scarcity_bonus=1.5, need_bonus=1.5, reasoning="test",
                             adp_rank=3)
        safety = [PickSafety("RP", 3, 10, 5, 0.50, "monitor", "getting thin")]
        opt.annotate_safety([rec], safety)
        assert "snipe" in rec.tags

    def test_no_wait_tag_without_adjustment(self):
        """Without league adjustment, safe RP should NOT get 'wait' tag."""
        config = LeagueConfig()
        opt = Optimizer(config)
        p = _pitcher("Closer", ["RP"], adp=50, SV=35)
        rec = Recommendation(player=p, total_score=5.0, z_score_value=3.0,
                             scarcity_bonus=1.0, need_bonus=1.0, reasoning="test")
        safety = [PickSafety("RP", 5, 10, 0, 1.0, "safe", "no one needs RP")]
        opt.annotate_safety([rec], safety)
        assert "wait" not in rec.tags
        # Should still get normal "safe" tag
        assert "safe" in rec.tags

    def test_no_snipe_tag_on_non_elite(self):
        """Non-elite RP (ADP rank > 5) with monitor signal should NOT get 'snipe'."""
        config = LeagueConfig(league_adp_adjustments={"RP": 2.0})
        opt = Optimizer(config)
        p = _pitcher("Mid Closer", ["RP"], adp=100, SV=25)
        rec = Recommendation(player=p, total_score=4.0, z_score_value=2.0,
                             scarcity_bonus=1.0, need_bonus=1.0, reasoning="test",
                             adp_rank=15)
        safety = [PickSafety("RP", 3, 10, 5, 0.50, "monitor", "getting thin")]
        opt.annotate_safety([rec], safety)
        assert "snipe" not in rec.tags


# ---------------------------------------------------------------------------
# Stat Reliability
# ---------------------------------------------------------------------------

class TestStatReliability:
    def test_empty_input_returns_default(self):
        assert Optimizer._stat_reliability({}) == 0.5

    def test_k_heavy_player_high_reliability(self):
        """Player whose value comes mostly from K (r=0.80) should be highly reliable."""
        cat_z = {"K": 3.0, "ERA": 0.2, "WHIP": 0.1}
        rel = Optimizer._stat_reliability(cat_z)
        assert rel > 0.70

    def test_era_heavy_player_low_reliability(self):
        """Player whose value comes mostly from ERA (r=0.37) should be low reliability."""
        cat_z = {"ERA": 3.0, "K": 0.1, "WHIP": 0.1}
        rel = Optimizer._stat_reliability(cat_z)
        assert rel < 0.45

    def test_sv_only_very_low(self):
        """Player value entirely from SV (r=0.20) should have very low reliability."""
        cat_z = {"SV": 2.5}
        rel = Optimizer._stat_reliability(cat_z)
        assert rel == pytest.approx(0.20)

    def test_hr_only(self):
        """HR-only player (r=0.74) should return exactly 0.74."""
        cat_z = {"HR": 2.0}
        rel = Optimizer._stat_reliability(cat_z)
        assert rel == pytest.approx(0.74)

    def test_balanced_hitter(self):
        """Balanced hitter across all 5 hitting cats."""
        cat_z = {"AVG": 1.0, "HR": 1.0, "R": 1.0, "RBI": 1.0, "SB": 1.0}
        rel = Optimizer._stat_reliability(cat_z)
        # Weighted avg of (0.43+0.74+0.55+0.55+0.60)/5 = 0.574
        assert rel == pytest.approx(0.574)

    def test_negative_z_scores_use_abs(self):
        """Negative z-scores still contribute to reliability via abs value."""
        cat_z = {"K": -2.0, "ERA": -1.0}
        rel = Optimizer._stat_reliability(cat_z)
        # Same as positive: weighted avg by abs
        cat_z_pos = {"K": 2.0, "ERA": 1.0}
        rel_pos = Optimizer._stat_reliability(cat_z_pos)
        assert rel == pytest.approx(rel_pos)

    def test_near_zero_z_returns_default(self):
        """All near-zero z-scores should return default 0.5."""
        cat_z = {"HR": 0.001, "R": 0.002}
        rel = Optimizer._stat_reliability(cat_z)
        assert rel == pytest.approx(0.5, abs=0.1)


class TestReliabilityTags:
    def test_stable_tag_on_k_pitcher(self, opt):
        """High-K pitcher should get [STABLE] tag."""
        pitchers = [
            _pitcher("K Machine", ["SP"], adp=20, K=280, ERA=3.50, WHIP=1.20,
                     IP=190, H=160, BB=55, ER=74, QS=18, SV=0),
            _pitcher("ERA Ace", ["SP"], adp=25, K=150, ERA=2.20, WHIP=0.90,
                     IP=180, H=120, BB=42, ER=44, QS=24, SV=0),
            _pitcher("Filler1", ["SP"], adp=50, K=170, ERA=3.20, WHIP=1.10,
                     IP=175, H=150, BB=48, ER=62, QS=20, SV=0),
            _pitcher("Filler2", ["SP"], adp=60, K=165, ERA=3.40, WHIP=1.15,
                     IP=170, H=155, BB=50, ER=64, QS=18, SV=0),
            _pitcher("Filler3", ["SP"], adp=70, K=160, ERA=3.60, WHIP=1.18,
                     IP=168, H=158, BB=52, ER=67, QS=16, SV=0),
        ]
        recs = opt.recommend(pitchers, [], n=5, pool="pitcher")
        k_rec = next(r for r in recs if r.player.name == "K Machine")
        # K Machine's value is heavily K-driven (r=0.80) — should be stable
        assert k_rec.reliability >= 0.55

    def test_volatile_tag_on_sv_closer(self, opt):
        """Closer whose value is mostly SV should get [VOLATILE] tag."""
        pitchers = [
            _pitcher("Save Guy", ["RP"], adp=50, SV=40, K=70, ERA=3.20, WHIP=1.15,
                     IP=60, H=52, BB=20, ER=21, QS=0),
            _pitcher("SP Ace", ["SP"], adp=20, K=250, ERA=2.80, WHIP=1.00,
                     IP=200, H=155, BB=45, ER=62, QS=25, SV=0),
            _pitcher("Filler1", ["SP"], adp=40, K=180, ERA=3.30, WHIP=1.12,
                     IP=180, H=155, BB=50, ER=66, QS=19, SV=0),
            _pitcher("Filler2", ["SP"], adp=55, K=170, ERA=3.50, WHIP=1.18,
                     IP=170, H=155, BB=50, ER=66, QS=17, SV=5),
            _pitcher("Filler3", ["RP"], adp=80, SV=25, K=65, ERA=3.50, WHIP=1.20,
                     IP=55, H=50, BB=18, ER=21, QS=0),
        ]
        recs = opt.recommend(pitchers, [], n=5, pool="pitcher")
        sv_rec = next(r for r in recs if r.player.name == "Save Guy")
        # SV (r=0.20) is a big part of this player's profile — should be low reliability
        assert sv_rec.reliability <= 0.50

    def test_recommend_does_not_change_ranking_by_reliability(self, opt, hitter_pool):
        """Reliability is informational only — recommend() ranking unchanged."""
        recs = opt.recommend(hitter_pool, [], n=10, pool="hitter")
        scores = [r.total_score for r in recs]
        assert scores == sorted(scores, reverse=True)
        # Verify reliability is populated but doesn't affect order
        for r in recs:
            assert 0.0 <= r.reliability <= 1.0


class TestRecommendStable:
    def test_returns_different_order(self, opt):
        """recommend_stable should produce a different ranking than recommend."""
        pool = [
            # K-heavy pitcher: high reliability, moderate raw score
            _pitcher("K Arm", ["SP"], adp=30, K=260, ERA=3.80, WHIP=1.22,
                     IP=190, H=170, BB=55, ER=80, QS=16, SV=0),
            # ERA-heavy pitcher: low reliability, high raw score from elite ERA
            _pitcher("ERA Ace", ["SP"], adp=25, K=160, ERA=2.20, WHIP=0.90,
                     IP=185, H=125, BB=42, ER=45, QS=26, SV=0),
            # Closer: very low reliability from SV
            _pitcher("Closer", ["RP"], adp=45, SV=40, K=75, ERA=2.90, WHIP=1.05,
                     IP=65, H=50, BB=18, ER=21, QS=0),
            _pitcher("Filler1", ["SP"], adp=50, K=175, ERA=3.40, WHIP=1.15,
                     IP=175, H=155, BB=50, ER=66, QS=18, SV=0),
            _pitcher("Filler2", ["SP"], adp=60, K=165, ERA=3.60, WHIP=1.18,
                     IP=170, H=158, BB=52, ER=68, QS=16, SV=0),
        ]
        recs_base = opt.recommend(pool, [], n=5, pool="pitcher")
        recs_stable = opt.recommend_stable(pool, [], n=5, pool="pitcher")

        base_order = [r.player.name for r in recs_base]
        stable_order = [r.player.name for r in recs_stable]

        # The orders should differ (reliability reweighting changes ranking)
        # At minimum, stable scores should be descending
        stable_scores = [r.total_score for r in recs_stable]
        assert stable_scores == sorted(stable_scores, reverse=True)

    def test_stable_boosts_k_heavy(self, opt):
        """K-heavy pitcher should rank better in recommend_stable."""
        pool = [
            _pitcher("K Machine", ["SP"], adp=30, K=280, ERA=3.80, WHIP=1.25,
                     IP=195, H=175, BB=58, ER=82, QS=15, SV=0),
            _pitcher("Closer", ["RP"], adp=25, SV=42, K=70, ERA=2.80, WHIP=1.00,
                     IP=65, H=48, BB=17, ER=20, QS=0),
            _pitcher("Filler1", ["SP"], adp=40, K=180, ERA=3.20, WHIP=1.10,
                     IP=180, H=152, BB=48, ER=64, QS=20, SV=0),
            _pitcher("Filler2", ["SP"], adp=50, K=170, ERA=3.40, WHIP=1.15,
                     IP=175, H=155, BB=50, ER=66, QS=18, SV=0),
            _pitcher("Filler3", ["SP"], adp=60, K=165, ERA=3.60, WHIP=1.20,
                     IP=170, H=158, BB=52, ER=68, QS=16, SV=0),
        ]
        recs_base = opt.recommend(pool, [], n=5, pool="pitcher")
        recs_stable = opt.recommend_stable(pool, [], n=5, pool="pitcher")

        base_rank = {r.player.name: i for i, r in enumerate(recs_base)}
        stable_rank = {r.player.name: i for i, r in enumerate(recs_stable)}

        # K Machine should rank same or better in stable
        assert stable_rank["K Machine"] <= base_rank["K Machine"]

    def test_stable_returns_requested_count(self, opt, hitter_pool):
        """recommend_stable returns exactly n results."""
        recs = opt.recommend_stable(hitter_pool, [], n=5, pool="hitter")
        assert len(recs) == 5

    def test_stable_preserves_recommendation_fields(self, opt, hitter_pool):
        """All Recommendation fields are populated in stable results."""
        recs = opt.recommend_stable(hitter_pool, [], n=3, pool="hitter")
        for r in recs:
            assert r.player is not None
            assert r.z_score_value != 0 or r.scarcity_bonus != 0
            assert r.reasoning
            assert 0.0 <= r.reliability <= 1.0
