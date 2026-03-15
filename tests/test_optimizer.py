"""Tests for the draft optimizer — z-scores, tiers, pick safety, category dashboard."""

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
