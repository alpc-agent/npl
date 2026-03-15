# League RP Draft Tendency — Proposal

## Observation
Your league drafts RPs later than consensus ADP suggests. This is a persistent league-specific trend.

## What to Build

### Step 0. Extract 2025 Pitcher Draft History from the Google Sheet

The 2025 draft sheet is already wired up in `league.json`:
- **Sheet ID:** `1bhs-QzmunMDLWxT3GvRvLg4l1M_8bgheiu2FC43D74E`
- **Pitcher Selections GID:** `1822798668`
- **Hitter Selections GID:** still needed (check the tab URL)
- **Owners (draft order):** Daniel, Hayden, Jack, Ethan, Luke, Muppy, Vik, Matt, Connor, Jacob, Andrew, Sam

Use `DraftSheetReader` to fetch the 2025 pitcher picks. The reader already handles
this layout — just point it at the 2025 sheet and call `fetch_all_picks()`.

**Only pitcher picks are needed for the RP discount analysis.**

```python
from drafter.sheets import DraftSheetReader

reader = DraftSheetReader(
    sheet_id="1bhs-QzmunMDLWxT3GvRvLg4l1M_8bgheiu2FC43D74E",
    hitter_gid="???",  # fill in once known
    pitcher_gid="1822798668",
)
picks = reader.fetch_all_picks()
# picks is a list of SheetPick(player_name, owner, round_number, pool)
```

Then transform into `data/draft_history.json`.

### 1. League Draft History File (`data/draft_history.json`)

Store last year's actual draft results so the optimizer can learn from league-specific tendencies. Structure:

```json
{
  "season": 2025,
  "picks": [
    {"round": 1, "pick": 1, "player": "Player Name", "owner": "OwnerName", "position": "SP"},
    {"round": 5, "pick": 52, "player": "Mason Miller", "owner": "SomeOwner", "position": "RP"}
  ]
}
```

Only pitcher picks are needed for the RP analysis. Cross-reference player names against
`data/players.json` to resolve SP vs RP position.

### 2. League ADP Adjustment in `config.py`

Add a `league_adp_adjustments` dict to `LeagueConfig` that maps positions to round-shift factors:

```python
league_adp_adjustments: dict[str, float]  # e.g. {"RP": +2.5} means RPs go ~2.5 rounds later in this league
```

This can be computed from `draft_history.json` (actual pick vs consensus ADP) or set manually.

**Current LeagueConfig fields** (for reference): `num_teams`, `draft_type`,
`hitting_categories`, `pitching_categories`, `inverse_categories`,
`category_weights`, `roster_slots`. Add `league_adp_adjustments` with
`field(default_factory=dict)`.

### 3. Pick Safety Adjustment in `optimizer.py`

The biggest practical impact: **pick_safety currently assumes other teams draft positions uniformly based on unfilled slots.** If your league consistently delays RPs, the threat model should reflect that:

- In `pick_safety()` (lines ~886-898), apply a `league_adp_adjustment` multiplier to the per-team `pick_rate` for RP. If RPs go 2+ rounds later in your league, the probability of someone sniping an RP before your turn drops significantly.
- Current pick_rate logic: `pick_rate = 1.0 / max(unfilled, 1)` — multiply this by a discount factor for positions with league adjustments.
- This makes RP show as `[SAFE]` more often (correctly), and lets you wait longer.

### 4. "RP Discount Window" Signal in Recommendations

Build on the existing `annotate_safety()` method (lines ~136-171) which already attaches
`[safe]` and `[reach]` tags. Add two new tags:

- `[WAIT]` — for RPs when pick safety is `[SAFE]` and league history says they'll slide
- `[SNIPE]` — when an elite RP (top-tier) is available at a discount the league won't contest

The key insight: **the optimal counter-strategy depends on your roster needs:**

- **If you need SV:** Don't draft early to "counter" the trend — instead, **exploit it**. Wait as long as pick_safety allows, and you'll get RP1/RP2 caliber closers at RP5/RP6 prices. Your league's tendency is a market inefficiency that benefits patient managers.
- **If you're punting SV:** The league tendency is irrelevant — skip RPs entirely.

### 5. Implementation Plan

**Step-by-step:**
1. Extract 2025 pitcher draft from the Google Sheet (Step 0 above)
2. Save to `data/draft_history.json`
3. Compute the RP round-shift from draft history vs ADP
4. Add `league_adp_adjustments` field to `LeagueConfig` in `config.py` (~5 lines)
5. Apply adjustment in `pick_safety()` pick_rate calculation in `optimizer.py` (~15 lines)
6. Add WAIT/SNIPE tag logic in `annotate_safety()` in `optimizer.py` (~20 lines)
7. Test with mock draft state to verify RP signals change appropriately

**Files to modify:**
- `src/drafter/config.py` — Add `league_adp_adjustments` field to `LeagueConfig`
- `src/drafter/optimizer.py` — Adjust `pick_safety()` pick_rate; extend `annotate_safety()` with WAIT/SNIPE
- `data/draft_history.json` — New file, populated from the 2025 sheet

**Files to create:**
- None beyond the data file

**Estimated changes:** ~40 lines in optimizer.py, ~5 lines in config.py, 1 new data file.

### 6. Strategy Answer: Should You Counter by Drafting RPs Early?

**No — you should exploit the inefficiency, not counter it.**

If everyone else waits on RPs, that means elite closers (Mason Miller, Edwin Díaz, Andrés Muñoz) will slide to you. The math:
- Miller's consensus ADP is pitcher #11 (overall ~44)
- If your league pushes RPs 2+ rounds later, he falls to ~pitcher #17-20 range
- You get a 32-save, 2.51 ERA closer at the cost of a mid-tier SP

The optimal play: **wait on RPs until pick_safety flags them as [MONITOR], then grab the best available.** You'll get better value than reaching early, because no one is competing with you for them.

The only exception: if you're at a snake draft turn (back-to-back picks) and an elite RP is there alongside a hitter/SP you want — grab both. Free value.
