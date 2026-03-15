# Fantasy Baseball Draft Optimizer

## Project Overview
AI-powered draft assistant for a 12-team H2H categories fantasy baseball league on ESPN.
Categories: AVG, HR, R, RBI, SB / QS, SV, K, ERA, WHIP

The user interacts with this tool through Claude Code conversations during their live draft.

## How to Use During Draft

All commands run from the project root (`~/npl`). Use `python3 -c` with inline scripts.

### 1. Start a Draft Session
```python
import sys; sys.path.insert(0, 'src')
from drafter.draft import Draft
from drafter.config import LeagueConfig
from drafter.optimizer import Optimizer

d = Draft()
config = LeagueConfig()
opt = Optimizer(config)

# First time only — set up your team:
d.setup("MyTeam", draft_position=7)  # Adjust position as needed
```

### 2. Log Picks As They Happen
```python
# When another team picks:
d.pick("Shohei Ohtani")          # Auto-assigns to current picking team
d.pick("Aaron Judge")

# If you need to specify a team:
d.pick("Bobby Witt Jr.", "Team 3")

# Undo a mistake:
d.undo()
```

### 3. Get Recommendations When It's Your Turn
```python
available = d.available()
my_roster = d.my_roster_players()
recs = opt.recommend(available, my_roster, list(d.players.values()), n=10)

for i, r in enumerate(recs, 1):
    pos = '/'.join(r.player.positions)
    print(f"{i}. {r.player.name} ({pos}, {r.player.team}) — Score: {r.total_score}")
    print(f"   Value: {r.z_score_value} | Scarcity: {r.scarcity_bonus} | Need: {r.need_bonus}")
    print(f"   {r.reasoning}")
```

### 4. Check Your Roster & Needs
```python
# View roster
for pick in d.my_roster():
    p = d.players[pick.player_name]
    print(f"  Rd {pick.round_number}: {p.name} ({'/'.join(p.positions)})")

# Category projections
totals = opt.analyze_roster(d.my_roster_players())
for cat, val in totals.items():
    print(f"  {cat}: {val}")
```

### 5. Browse Available Players by Position
```python
for p in d.available(position="SS", limit=10):
    print(f"  {p.adp:>5.0f}  {p.name:<25} {p.team}")

# Valid positions: C, 1B, 2B, 3B, SS, OF, DH, SP, RP, CI, MI, P
```

### 6. Check Draft Status
```python
print(d.status())
```

## Key Files
- `data/players.json` — 933 players with projections from Mr. Cheatsheet's Special Blend
- `draft_state.json` — Live draft state (auto-saved after each pick)
- `src/drafter/config.py` — League settings (categories, roster slots)
- `src/drafter/draft.py` — Draft engine (pick logging, state management)
- `src/drafter/optimizer.py` — Ranking engine (z-scores, scarcity, needs)
- `src/drafter/models.py` — Data models
- `src/drafter/import_excel.py` — Excel import (run once to seed data)

## Roster Slots
C(1), 1B(1), 2B(1), 3B(1), SS(1), OF(5), DH(1), CI(1), MI(1), SP(5), RP(2), P(1), Bench(8)

## Draft Strategy Notes
- **QS over W**: Prioritize pitchers with high QS projections (innings eaters, low ERA SPs) over win-dependent arms
- **Positional scarcity**: C and SS tend to be thinnest; OF is deepest
- **Category balance**: In H2H, avoid punting categories since you need to win 5+ cats each week
- **ERA/WHIP**: Rate stats — consider IP volume when evaluating pitchers
- **Snake draft**: If picking late in a round, you get back-to-back picks at the turn

## Re-importing Player Data
If the Excel file is updated:
```bash
python3 src/drafter/import_excel.py /path/to/cheatsheet.xlsm data/players.json
```
