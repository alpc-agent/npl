# Fantasy Baseball Draft Optimizer

## Project Overview
AI-powered draft assistant for a 12-team H2H categories fantasy baseball league on ESPN.
Categories: AVG, HR, R, RBI, SB / QS, SV, K, ERA, WHIP

The user interacts with this tool through Claude Code conversations during their live draft.

## Draft Day Workflow (iMessage Integration)

The draft runs through the "NPL raft⚾️" iMessage group chat. Claude reads new messages,
proposes who was picked, and **waits for user confirmation before logging any pick**.

### 1. Initialize the Draft Session
```python
import sys; sys.path.insert(0, 'src')
import json
from drafter.draft import Draft
from drafter.config import LeagueConfig
from drafter.optimizer import Optimizer
from drafter.imessage import DraftChatMonitor

d = Draft()
config = LeagueConfig()
opt = Optimizer(config)

with open('league.json') as f:
    league = json.load(f)

monitor = DraftChatMonitor(
    player_names=list(d.players.keys()),
    team_map=league['team_map'],
)

# First time — set up team (update draft_position and team name in league.json first):
d.setup(league['team_map']['me'], draft_position=league['draft_position'],
        team_names=league['draft_order'])
```

### 2. Check for New Picks (CONFIRMATION REQUIRED)

When the user says "check the chat", "any new picks?", "what's happening?", etc.:

```python
results = monitor.check_new_messages()
```

For each result where `result['player']` is not None, **present the proposed pick to the user**:
- Show: "[sender/team] said: '[message text]' — Is this **[matched player name]**?"
- Wait for user to confirm YES or NO
- Only call `d.pick(player_name, team_name)` after confirmation
- If NO, ask the user who was actually picked, or skip (it was just chat)

For messages with no player match, show them as chat noise (no action needed).

**CRITICAL**: Never auto-log picks. Always confirm with the user first.

### 3. Get Recommendations When It's My Turn
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

### 4. Manual Pick Entry
If the parser misses a pick or you need to enter one manually:
```python
d.pick("Player Name")              # Auto-assigns to current picking team
d.pick("Player Name", "Team 3")    # Specific team
d.undo()                           # Undo last pick
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
for p in d.available(position="SS", limit=10):
    print(f"  {p.adp:>5.0f}  {p.name:<25} {p.team}")
# Valid positions: C, 1B, 2B, 3B, SS, OF, DH, SP, RP, CI, MI, P
```

### 7. Draft Status
```python
print(d.status())
```

## Pre-Draft Setup (league.json)

Before the draft, update `league.json` with:
1. `draft_position` — your pick number (1-12)
2. `team_map` — map each phone number to a team/owner name
3. `draft_order` — list of team names in draft order (pick 1 first)

## Key Files
- `data/players.json` — 933 players with projections from Mr. Cheatsheet's Special Blend
- `league.json` — League config: team mapping, draft order, chat ID
- `draft_state.json` — Live draft state (auto-saved after each pick)
- `imessage_state.json` — Tracks last-read message ID
- `src/drafter/imessage.py` — iMessage reader & parser
- `src/drafter/draft.py` — Draft engine (pick logging, state management)
- `src/drafter/optimizer.py` — Ranking engine (z-scores, scarcity, needs)
- `src/drafter/config.py` — League settings (categories, roster slots)
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
