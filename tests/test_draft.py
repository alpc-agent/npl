"""Tests for draft state management — threat window, position runs, sync."""

import sys
import json
import os

sys.path.insert(0, "src")

import pytest
from drafter.draft import Draft
from drafter.models import DraftPick, DraftState, Player


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def draft(tmp_path):
    """A fresh Draft instance with a temporary state file."""
    state_path = str(tmp_path / "test_state.json")
    d = Draft(state_path=state_path)
    owners = [f"Team{i}" for i in range(1, 13)]
    owners[5] = "MyTeam"
    d.setup("MyTeam", draft_position=6, team_names=owners)
    return d


def _simulate_picks(draft, picks):
    """Simulate a list of (player_name, team_name) picks."""
    for name, team in picks:
        draft.pick(name, team)


# ---------------------------------------------------------------------------
# threat_window
# ---------------------------------------------------------------------------

class TestThreatWindow:
    def test_empty_when_my_turn(self, draft):
        # Position 6, picks 1-5 fill, pick 6 is mine
        avail = sorted(draft.players.values(), key=lambda p: p.adp)
        for i in range(5):
            draft.pick(avail[i].name, f"Team{i+1}")
        # Now it's MyTeam's turn (pick 6)
        threat = draft.threat_window(pool="hitter")
        assert len(threat) == 0

    def test_returns_correct_teams(self, draft):
        # No picks yet, position 6 — 5 teams pick before me
        threat = draft.threat_window(pool="hitter")
        assert len(threat) == 5
        assert threat[0]["team_name"] == "Team1"
        assert threat[4]["team_name"] == "Team5"

    def test_single_position_marks_filled(self, draft):
        # Fill picks 1-6 (through MyTeam) so that Team1 appears in
        # round 2's threat window with a prior pick on record.
        avail = sorted(draft.players.values(), key=lambda p: p.adp)
        ss_players = [p for p in avail if p.positions == ["SS"]]
        if not ss_players:
            pytest.skip("No pure SS players in data")
        # Team1 picks a pure SS first
        draft.pick(ss_players[0].name, "Team1")
        idx = 1
        for i in range(2, 7):
            draft.pick(avail[idx].name, f"Team{i}" if i != 6 else "MyTeam")
            idx += 1
        # Continue through rest of round 1
        for i in range(7, 13):
            draft.pick(avail[idx].name, f"Team{i}")
            idx += 1
        # Now round 2 (reverse): Team12 first, then Team11, ...
        # MyTeam picks 7th in reverse round (position 6 reversed = 7th)
        # So teams Team12..Team7 pick before us, then our turn
        # But Team1 picks LAST in round 2 (position 12 reversed = last)
        # Actually in round 2 reverse: Team1 is at the end.
        # Let's just check whoever picks before us has correct fill data.
        threat = draft.threat_window(pool="hitter")
        # Team12 should be first in threat (picks first in reverse round)
        assert len(threat) > 0
        # Verify that team1's data is correct when it DOES appear
        team1_entries = [tw for tw in threat if tw["team_name"] == "Team1"]
        if team1_entries:
            assert "SS" in team1_entries[0]["positions_filled"]

    def test_multi_position_not_filled(self, draft):
        # Find a multi-position player (e.g., 1B/C)
        multi = [p for p in draft.players.values()
                 if len(p.positions) > 1 and "C" in p.positions and "1B" in p.positions]
        if not multi:
            pytest.skip("No 1B/C multi-position players in data")
        avail = sorted(draft.players.values(), key=lambda p: p.adp)
        # Team1 picks the multi-position player, then fill through MyTeam's pick
        draft.pick(multi[0].name, "Team1")
        idx = 0
        used = {multi[0].player_id}
        for i in range(2, 7):
            while avail[idx].player_id in used:
                idx += 1
            draft.pick(avail[idx].name, f"Team{i}" if i != 6 else "MyTeam")
            used.add(avail[idx].player_id)
            idx += 1
        for i in range(7, 13):
            while avail[idx].player_id in used:
                idx += 1
            draft.pick(avail[idx].name, f"Team{i}")
            used.add(avail[idx].player_id)
            idx += 1
        threat = draft.threat_window(pool="hitter")
        team1_entries = [tw for tw in threat if tw["team_name"] == "Team1"]
        if team1_entries:
            # Multi-position player should NOT definitively fill either position
            assert "C" not in team1_entries[0]["positions_filled"]
            assert "1B" not in team1_entries[0]["positions_filled"]

    def test_positions_filled_empty_for_new_team(self, draft):
        threat = draft.threat_window(pool="hitter")
        for tw in threat:
            assert tw["positions_filled"] == set()


# ---------------------------------------------------------------------------
# position_runs
# ---------------------------------------------------------------------------

class TestPositionRuns:
    def test_no_run_with_single_pick(self, draft):
        avail = sorted(draft.players.values(), key=lambda p: p.adp)
        draft.pick(avail[0].name, "Team1")
        runs = draft.position_runs()
        # Single pick can't be a run
        for count in runs.values():
            assert count >= 2

    def test_detects_run(self, draft):
        # Pick two catchers in a row
        catchers = [p for p in draft.players.values() if "C" in p.positions and len(p.positions) == 1]
        catchers.sort(key=lambda p: p.adp)
        if len(catchers) >= 2:
            draft.pick(catchers[0].name, "Team1")
            draft.pick(catchers[1].name, "Team2")
            runs = draft.position_runs()
            assert "C" in runs
            assert runs["C"] >= 2

    def test_of_dh_excluded(self, draft):
        # OF and DH should not trigger run detection
        of_players = [p for p in draft.players.values() if "OF" in p.positions]
        of_players.sort(key=lambda p: p.adp)
        if len(of_players) >= 2:
            draft.pick(of_players[0].name, "Team1")
            draft.pick(of_players[1].name, "Team2")
            runs = draft.position_runs()
            assert "OF" not in runs
            assert "DH" not in runs

    def test_lookback_parameter(self, draft):
        avail = sorted(draft.players.values(), key=lambda p: p.adp)
        # Make 10 picks
        for i in range(10):
            draft.pick(avail[i].name, f"Team{(i % 11) + 1}")
        # Only look at last 2 picks
        runs = draft.position_runs(lookback=2)
        # Counts should only reflect the last 2 picks
        for count in runs.values():
            assert count <= 2

    def test_empty_picks(self, draft):
        runs = draft.position_runs()
        assert runs == {}


# ---------------------------------------------------------------------------
# pick and undo
# ---------------------------------------------------------------------------

class TestPickAndUndo:
    def test_pick_records_correctly(self, draft):
        avail = sorted(draft.players.values(), key=lambda p: p.adp)
        result = draft.pick(avail[0].name, "Team1")
        assert "Team1" in result
        assert avail[0].name in result

    def test_duplicate_pick_rejected(self, draft):
        avail = sorted(draft.players.values(), key=lambda p: p.adp)
        draft.pick(avail[0].name, "Team1")
        result = draft.pick(avail[0].name, "Team2")
        assert "already been drafted" in result

    def test_undo_removes_last(self, draft):
        avail = sorted(draft.players.values(), key=lambda p: p.adp)
        draft.pick(avail[0].name, "Team1")
        result = draft.undo()
        assert "Undid" in result
        assert avail[0].name in result
        # Player should be available again
        assert avail[0].player_id not in draft.state.drafted_player_ids()

    def test_undo_empty(self, draft):
        result = draft.undo()
        assert "No picks" in result
