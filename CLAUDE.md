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
config = LeagueConfig()  # balanced (default)
# Or use a strategy: config = LeagueConfig.with_strategy("punt_sb")
# Options: "balanced", "punt_sb", "punt_sv", "punt_avg"
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
    tags = ' '.join(f'[{t.upper()}]' for t in r.tags)
    tag_str = f' {tags}' if tags else ''
    print(f"{i}. {r.player.name} ({pos}){tag_str} — AI: {r.total_score} | Cheatsheet: #{r.adp_rank}")
    print(f"   Tier: {tier}")
    print(f"   Value: {r.z_score_value} | Scarcity: {r.scarcity_bonus} | Need: {r.need_bonus}")
    print(f"   {r.reasoning}")
```

### 3b. Pick Safety — Should I Wait or Reach?
Pick safety analyzes every pick between now and your next turn. It checks which
teams are picking, what positions they've already filled, and how many viable
tier players remain — then flags positions as `[SAFE]`, `[MONITOR]`, or `[REACH]`.

Use this when deciding whether to draft a position now or wait a round.

```python
# Get pick safety for hitter positions:
available = d.available(pool="hitter")
my_roster = d.my_roster_players()
threat = d.threat_window(pool="hitter")
safety = opt.pick_safety(available, my_roster, threat, pool="hitter")

for s in safety:
    print(f"  {s.position}: [{s.signal.upper()}] ({s.prob_available:.0%}) — {s.detail}")

# Annotate recommendations with safety flags:
recs = opt.recommend(available, my_roster, n=10, pool="hitter")
opt.annotate_safety(recs, safety)

# Safety flags appear in tags as [SAFE] or [REACH]
# and in r.safety_flags for detailed per-position info

# Check for position runs (2+ same position in last 6 picks):
runs = d.position_runs()
for pos, count in runs.items():
    print(f"  {pos}: {count} drafted recently — RUN IN PROGRESS")
```

**How it works:**
- The threat window covers ALL picks between now and your next turn (snake-aware)
- For each unfilled position, it counts how many teams ahead still need that position
- Each team's pick rate is based on how many total positions they still need to fill
  (a team with 2 unfilled positions is much more likely to pick at yours than one with 8)
- Probability uses binomial DP to compute P(at least one viable tier player survives)
- Signals: `[SAFE]` (>70%), `[MONITOR]` (35-70%), `[REACH]` (<35%)
- Position run detection flags when 2+ players at the same position drafted in recent picks

### 3c. Quick Board — "it's my pick"
The `/my-pick` skill (`.claude/skills/my-pick/SKILL.md`) consolidates recommendations,
cheatsheet consensus, pick safety, and league-relative dashboard into a single output.
Just say "it's my hitter pick" or "pitcher pick" and it runs the full pipeline.

The league-relative dashboard compares your category projections to the average of
all 12 teams' current rosters:
```python
rel = opt.league_relative_dashboard(d.my_roster_players(), d)
# Returns: my_projections, league_avg, deltas (%), hint
```

### 4. Manual Pick Entry
If the sheet is behind or you need to enter a pick manually:
```python
d.pick("Player Name", "OwnerName")
d.undo()  # Undo last pick
```

### 5. Check Roster & Category Dashboard
```python
# My roster
for pick in d.my_roster():
    p = d.players[pick.player_name]
    print(f"  Rd {pick.round_number}: {p.name} ({'/'.join(p.positions)})")

# Category dashboard — shows projections + grades + strategy hint
dash = opt.category_dashboard(d.my_roster_players())
for cat in config.all_categories:
    grade = dash['grades'][cat]
    proj = dash['projections'][cat]
    print(f"  {cat:>4}: {proj:>8}  [{grade}]")
if dash['strategy_hint']:
    print(f"  >> {dash['strategy_hint']}")
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

## Pre-Draft Player Tags

`data/tags.json` stores editorial tags for rookies, breakout candidates, and sleepers.
These come from web research of current expert consensus — **never guess names**.

To refresh before a draft:
1. Search for "2026 fantasy baseball rookies", "breakout candidates", "sleepers"
2. Cross-reference names against `data/players.json` (fuzzy matching handles accents)
3. Update `data/tags.json` with verified names

Tags appear in recommendations as `[ROOKIE]`, `[BREAKOUT]`, `[SLEEPER]`, `[VALUE]`.
The `value` tag is computed at runtime (ADP rank 15+ spots worse than AI rank).

## Key Files
- `data/players.json` — 933 players with projections from Mr. Cheatsheet's Special Blend
- `data/tags.json` — Player tags: rookies, breakout candidates, sleepers (from web research)
- `league.json` — League config: sheet IDs, owner names, draft position
- `draft_state.json` — Live draft state (auto-saved, .gitignored)
- `src/drafter/sheets.py` — Google Sheets reader (CSV export, no auth needed)
- `src/drafter/draft.py` — Draft engine (pick logging, sheet sync, state management)
- `src/drafter/optimizer.py` — Ranking engine (z-scores, scarcity, needs, tiers, pick safety, category dashboard)
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
- **Category strategy**: Use `LeagueConfig.with_strategy("punt_sb")` etc. to slant toward
  dominating 5-6 categories. Safest punts: SB, SV. Check `opt.category_dashboard()` to see
  emerging strengths and decide when to commit
- **ERA/WHIP**: Rate stats — consider IP volume when evaluating pitchers
- **Snake draft**: If picking late in a round, you get back-to-back picks at the turn
- **Keepers**: 3 keepers per team at a discounted round — factor in that top players may be kept

## Re-importing Player Data
If the Excel file is updated:
```bash
python3 src/drafter/import_excel.py /path/to/cheatsheet.xlsm data/players.json
```
