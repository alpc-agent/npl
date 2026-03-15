# Fantasy Baseball Draft Optimizer

## Project Overview
AI-powered draft assistant for a 12-team H2H categories fantasy baseball league on ESPN.
Categories: AVG, HR, R, RBI, SB / QS, SV, K, ERA, WHIP

The user interacts with this tool through Claude Code conversations during their live draft.

## Draft Day Workflow (Google Sheets Integration)

The draft is tracked via a shared Google Sheet with Hitter Selections and Pitcher Selections tabs.
The sheet is the source of truth — picks are applied automatically on sync.

**IMPORTANT: Hitters and pitchers are drafted in separate pools.**
When the user asks "who should I pick?", always ask (or infer from context) whether
it's a hitter or pitcher pick, and pass `pool="hitter"` or `pool="pitcher"` to both
`d.available()` and `opt.recommend()`. Never mix the pools in recommendations.

### 1. Initialize the Draft Session
```python
import sys; sys.path.insert(0, 'src')
import json
from drafter.draft import Draft
from drafter.config import LeagueConfig
from drafter.optimizer import Optimizer
from drafter.sheets import DraftSheetReader

d = Draft()
config = LeagueConfig()
opt = Optimizer(config)

with open('league.json') as f:
    league = json.load(f)

reader = DraftSheetReader(
    sheet_id=league['sheet_id'],
    hitter_gid=league['hitter_selections_gid'],
    pitcher_gid=league['pitcher_selections_gid'],
)

# First time only — set up your team:
d.setup(league['my_team'], draft_position=league['draft_position'],
        team_names=league['owners'])
```

### 2. Sync Picks from the Google Sheet

When the user says "sync", "check the sheet", "what's new?", etc.:

```python
result = d.sync_from_sheet(reader)
```

**The sheet is the source of truth — picks are applied automatically.**
- Show count: "Applied X new picks, Y unmatched"
- `result['applied']` — list of confirmation messages for newly applied picks
- `result['unmatched']` — list of (SheetPick, error) for names that couldn't be resolved
- `result['already_drafted']` — count of picks already in state
- For unmatched: show the name from the sheet and ask user to resolve manually

**Keepers**: Players pre-filled in the sheet before the draft starts are keepers.
They appear as regular picks in early rounds. The sync treats them the same —
they're drafted players occupying a roster spot and draft pick.

### 3. Get Recommendations When It's My Turn
**Always specify pool="hitter" or pool="pitcher"** since they are drafted separately.

Each recommendation shows both perspectives:
- **Cheatsheet Rank (`adp_rank`)**: Mr. Cheatsheet's objective consensus ranking among available players
- **AI Score (`total_score`)**: Our optimizer's score factoring in z-scores (rate-stat weighted),
  positional scarcity, category needs, and tier depletion urgency
- **Tier info**: Position-based tier (e.g., "Tier 1 SS (3 left)") with REACH warnings when
  a tier is nearly depleted
```python
# For a hitter pick:
available = d.available(pool="hitter")
my_roster = d.my_roster_players()
recs = opt.recommend(available, my_roster, n=10, pool="hitter")

# For a pitcher pick:
available = d.available(pool="pitcher")
recs = opt.recommend(available, my_roster, n=10, pool="pitcher")

for i, r in enumerate(recs, 1):
    pos = '/'.join(r.player.positions)
    tier = r.best_tier.tier_label if r.best_tier else '-'
    print(f"{i}. {r.player.name} ({pos}) — AI Score: {r.total_score} | Cheatsheet Rank: #{r.adp_rank}")
    print(f"   Tier: {tier}")
    print(f"   Value: {r.z_score_value} | Scarcity: {r.scarcity_bonus} | Need: {r.need_bonus}")
    print(f"   {r.reasoning}")
```

### 4. Manual Pick Entry
If the sheet is behind or you need to enter a pick manually:
```python
d.pick("Player Name", "OwnerName")
d.undo()  # Undo last pick
```

### 5. Check Roster & Projections
```python
# My roster
for pick in d.my_roster():
    p = d.players[pick.player_name]
    print(f"  Rd {pick.round_number}: {p.name} ({'/'.join(p.positions)})")

# Category projections
totals = opt.analyze_roster(d.my_roster_players())
for cat, val in totals.items():
    print(f"  {cat}: {val}")
```

### 6. Browse Available by Position
```python
for p in d.available(position="SS", pool="hitter", limit=10):
    print(f"  {p.adp:>5.0f}  {p.name:<25} {p.team}")
# Hitter positions: C, 1B, 2B, SS, 3B, IF, LF, CF, RF, OF, DH
# Pitcher positions: SP, RP
```

### 7. Draft Status
```python
print(d.status())
```

## Pre-Draft Setup (league.json)

Before the draft, update `league.json` with:
1. `sheet_id` — the Google Sheet ID (from the URL: docs.google.com/spreadsheets/d/**THIS_PART**/edit)
2. `hitter_selections_gid` — the gid for the Hitter Selections tab (from the URL: gid=**THIS_PART**)
3. `pitcher_selections_gid` — the gid for the Pitcher Selections tab
4. `my_team` — your owner name as it appears in the sheet
5. `draft_position` — your pick number (1-12)
6. `owners` — list of all owner names in draft order

The sheet must be shared as "Anyone with the link can view" for the CSV export to work.

## Key Files
- `data/players.json` — 933 players with projections from Mr. Cheatsheet's Special Blend
- `league.json` — League config: sheet IDs, owner names, draft position
- `draft_state.json` — Live draft state (auto-saved, .gitignored)
- `src/drafter/sheets.py` — Google Sheets reader (CSV export, no auth needed)
- `src/drafter/draft.py` — Draft engine (pick logging, sheet sync, state management)
- `src/drafter/optimizer.py` — Ranking engine (z-scores, scarcity, needs)
- `src/drafter/config.py` — League settings (categories, roster slots)
- `src/drafter/models.py` — Data models
- `src/drafter/import_excel.py` — Excel import (run once to seed data)

## Roster Slots (ESPN format)
Hitters: C, 1B, 2B, SS, 3B, IF(1 — any infielder), LF, CF, RF, OF(1 — any outfielder), DH
Pitchers: SP(5), RP(2)
Bench: Unlimited

## Draft Strategy Notes
- **QS over W**: Prioritize pitchers with high QS projections (innings eaters, low ERA SPs) over win-dependent arms
- **Positional scarcity**: C and SS tend to be thinnest; OF is deepest
- **Category balance**: In H2H, avoid punting categories since you need to win 5+ cats each week
- **ERA/WHIP**: Rate stats — consider IP volume when evaluating pitchers
- **Snake draft**: If picking late in a round, you get back-to-back picks at the turn
- **Keepers**: 3 keepers per team at a discounted round — factor in that top players may be kept

## Re-importing Player Data
If the Excel file is updated:
```bash
python3 src/drafter/import_excel.py /path/to/cheatsheet.xlsm data/players.json
```
