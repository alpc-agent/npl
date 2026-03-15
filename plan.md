# League RP Draft Tendency — Proposal

## Observation
Your league drafts RPs later than consensus ADP suggests. This is a persistent league-specific trend.

## What to Build

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

This needs to be populated manually (or from a sheet export) with last year's pitcher draft. Even just the pitcher picks would be valuable.

### 2. League ADP Adjustment in `config.py`

Add a `league_adp_adjustments` dict to `LeagueConfig` that maps positions to round-shift factors:

```python
league_adp_adjustments: dict[str, float]  # e.g. {"RP": +2.5} means RPs go ~2.5 rounds later in this league
```

This can be computed from `draft_history.json` (actual pick vs consensus ADP) or set manually.

### 3. Pick Safety Adjustment in `optimizer.py`

The biggest practical impact: **pick_safety currently assumes other teams draft positions uniformly based on unfilled slots.** If your league consistently delays RPs, the threat model should reflect that:

- In `pick_safety()`, apply a `league_adp_adjustment` multiplier to the per-team `pick_rate` for RP. If RPs go 2+ rounds later in your league, the probability of someone sniping an RP before your turn drops significantly.
- This makes RP show as `[SAFE]` more often (correctly), and lets you wait longer.

### 4. "RP Discount Window" Signal in Recommendations

Add a new tag `[WAIT]` for RPs when pick safety says you can safely delay. Conversely, add `[SNIPE]` when an elite RP is available and the league tendency means you can grab them at a discount others won't contest.

The key insight: **the optimal counter-strategy depends on your roster needs:**

- **If you need SV:** Don't draft early to "counter" the trend — instead, **exploit it**. Wait as long as pick_safety allows, and you'll get RP1/RP2 caliber closers at RP5/RP6 prices. Your league's tendency is a market inefficiency that benefits patient managers.
- **If you're punting SV:** The league tendency is irrelevant — skip RPs entirely.

### 5. Implementation Plan

**Files to modify:**
- `src/drafter/config.py` — Add `league_adp_adjustments` field to `LeagueConfig`
- `src/drafter/optimizer.py` — Adjust `pick_safety()` pick_rate calculation for positions with league adjustments; add WAIT/SNIPE tag logic
- `data/draft_history.json` — New file, populated with 2025 pitcher draft order

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
