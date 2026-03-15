---
name: my-pick
description: >-
  Draft recommendation board. Runs the full optimizer pipeline and displays
  a compact board with AI picks, cheatsheet consensus, pick safety, and
  league-relative dashboard. Triggers when the user says "it's my pick",
  "my turn", "who should I pick", "my pick", "recommend", "what should I
  draft", "hitter pick", "pitcher pick", or any variation indicating they
  want draft recommendations for their current turn.
---

# My Pick -- Draft Recommendation Board

When the user indicates it is their turn to pick, run the full recommendation
pipeline and display a single compact output block they can scan in seconds.

## Pool Handling

Hitters and pitchers are drafted in **separate pools**. Before running the
pipeline, determine the pool:

- If the user said "hitter pick", "hitter", or context is clear: `pool="hitter"`
- If the user said "pitcher pick", "pitcher", or context is clear: `pool="pitcher"`
- If ambiguous (e.g., just "it's my pick"): ask "Hitter or pitcher pick?"

## Pipeline

Run the following Python code. Objects `d` (Draft), `opt` (Optimizer), and
`config` (LeagueConfig) are already initialized per CLAUDE.md workflow.
Replace `POOL` with `"hitter"` or `"pitcher"`.

```python
POOL = "hitter"  # or "pitcher"

# 1. Sync (optional -- skip if reader not set up)
try:
    result = d.sync_from_sheet(reader)
    sync_msg = f"Synced {len(result['applied'])} new picks." if result['applied'] else None
except NameError:
    sync_msg = None

# 2. Gather data
available = d.available(pool=POOL)
my_roster = d.my_roster_players()
threat = d.threat_window(pool=POOL)
runs = d.position_runs()

# 3. Recommendations + safety
recs = opt.recommend(available, my_roster, n=5, pool=POOL)
safety = opt.pick_safety(available, my_roster, threat, pool=POOL)
opt.annotate_safety(recs, safety)

# 3b. Reliability-adjusted recommendations (separate ranking)
recs_stable = opt.recommend_stable(available, my_roster, n=5, pool=POOL)

# 4. Cheatsheet consensus (top 5 by ADP)
cheatsheet = sorted(available, key=lambda p: p.adp)[:5]

# 5. League-relative dashboard (vs last season's actual stats)
rel = opt.league_relative_dashboard(my_roster, available=d.available())

# 6. Draft context
status_pick = d.state.current_pick
status_round = d.state.current_round
picks_until = d.state.picks_until_mine()

# === FORMAT OUTPUT ===

pool_label = POOL.upper()
header = f"## {pool_label} PICK  |  Rd {status_round}, Pick {status_pick}"
if picks_until is not None and picks_until > 0:
    header += f"  |  {picks_until} picks until next turn"

# Alerts
alerts = []
for pos, count in runs.items():
    alerts.append(f"{pos} RUN ({count} recent)")
for s in safety:
    if s.signal == "reach":
        alerts.append(f"{s.position} tier depleting")
alert_line = f"**ALERTS:** {' | '.join(alerts)}" if alerts else ""

# AI recommendation table (with reliability column)
t_hdr = " #  Player              Pos    AI   ADP  Rel  Tier           Safety   Tags"
t_sep = "--- ------------------- ------ ---- ---- ---- -------------- -------- --------"
t_rows = []
for i, r in enumerate(recs, 1):
    name = r.player.name[:19].ljust(19)
    pos = '/'.join(r.player.positions)[:6].ljust(6)
    ai = f"{r.total_score:.2f}".rjust(4)[:4]
    adp = f"#{r.adp_rank}".rjust(4)
    rel_s = f"{r.reliability:.2f}"
    if r.best_tier:
        bt = r.best_tier
        tier_s = f"T{bt.tier} {bt.position} ({bt.remaining_in_tier} left)"[:14].ljust(14)
    else:
        tier_s = "-".ljust(14)
    if r.safety_flags:
        worst = min(r.safety_flags, key=lambda s: s.prob_available)
        saf = worst.signal.upper().ljust(8)
    else:
        saf = "-".ljust(8)
    tag_s = ' '.join(t.upper() for t in r.tags if t not in ('safe', 'reach'))
    t_rows.append(f" {i}  {name} {pos} {ai} {adp}  {rel_s} {tier_s} {saf} {tag_s}")

# Reliability-adjusted view (compact re-rank)
rs_hdr = " #  Player              Pos    Adj  Rel  Tags"
rs_sep = "--- ------------------- ------ ---- ---- --------"
rs_rows = []
for i, r in enumerate(recs_stable, 1):
    name = r.player.name[:19].ljust(19)
    pos = '/'.join(r.player.positions)[:6].ljust(6)
    adj = f"{r.total_score:.2f}".rjust(4)[:4]
    rel_s = f"{r.reliability:.2f}"
    tag_s = ' '.join(t.upper() for t in r.tags if t not in ('safe', 'reach'))
    rs_rows.append(f" {i}  {name} {pos} {adj} {rel_s} {tag_s}")

# Cheatsheet consensus
cs_parts = []
for i, p in enumerate(cheatsheet, 1):
    pos = '/'.join(p.positions)
    cs_parts.append(f"{i}. {p.name} ({pos})")
cs_line = "  ".join(cs_parts)

# Reasoning (top 3)
reason_lines = []
for i, r in enumerate(recs[:3], 1):
    last = r.player.name.split()[-1]
    reason_lines.append(f"{i}. {last} -- {r.reasoning}")

# Dashboard vs last season (scaled projections, rankings + deltas)
cats = config.hitting_categories if POOL == "hitter" else config.pitching_categories
dash_parts = []
for cat in cats:
    val = rel['scaled_projections'].get(cat, 0)
    rank = rel['rankings'].get(cat, '-')
    delta = rel['deltas'].get(cat, 0)
    sign = "+" if delta >= 0 else ""
    if cat in ('AVG', 'ERA', 'WHIP'):
        dash_parts.append(f"{cat} {val:.3f} #{rank} ({sign}{delta:.0f}%)")
    else:
        dash_parts.append(f"{cat} {val:.0f} #{rank} ({sign}{delta:.0f}%)")
dash_line = "  ".join(dash_parts)
hint_line = f"  Hint: {rel['hint']}" if rel['hint'] else ""

# Assemble
out = [header, ""]
if sync_msg:
    out += [f"*{sync_msg}*", ""]
if alert_line:
    out += [alert_line, ""]
out += ["**AI Ranking:**", t_hdr, t_sep] + t_rows + [""]
out += ["**Reliability-Adjusted (sticky stats rewarded):**", rs_hdr, rs_sep] + rs_rows + [""]
out += [f"**Cheatsheet consensus (what opponents see):**", f" {cs_line}", ""]
out += ["**Why:**"] + reason_lines + [""]
out += [f"**Dashboard vs Last Season:**", f"  {dash_line}"]
if hint_line:
    out.append(hint_line)
out += ["", "> Pick a player by name, or say \"pass\" to skip."]

print("\n".join(out))
```

## Output Rules

1. Print the formatted output exactly as produced. Do not add extra commentary
   or preamble around it. The block IS the response.

2. If sync found unmatched picks, append after the main block:
   `Unmatched: [names]. Resolve with d.pick("Name", "Owner").`

3. After showing the board, **wait for the user to name a player**. Do not
   auto-draft. When they name someone:
   - Run `d.pick("Player Name")` to log the pick
   - Confirm with the pick result message

4. If the user asks for more detail on a player (e.g., "tell me more about
   Henderson"), show the full reasoning, all safety_flags with detail strings,
   and the player's key projected stats from hitting/pitching_projections.

5. If the user says "more" or "show 10", re-run with `n=10` and display the
   expanded table.

## Formatting Notes

- No emojis unless the user asks.
- Table is designed for monospace / terminal rendering. Keep column alignment.
- Safety column = most urgent signal across player's positions (REACH > MONITOR > SAFE).
- Tags column excludes "safe"/"reach" (shown in Safety column). Shows editorial
  tags: ROOKIE, BREAKOUT, SLEEPER, VALUE, STABLE, VOLATILE, WAIT, SNIPE.
- **Rel** column shows the 0-1 reliability score. Higher = value from sticky stats
  (K r=0.80, HR r=0.74). Lower = value from volatile stats (SV r=0.20, ERA r=0.37).
- The **Reliability-Adjusted** section re-ranks by blending AI score with reliability.
  Compare against the AI ranking to see which players move up/down based on stat stickiness.
- Dashboard shows projected league rank (#1-#12) vs last season's actuals,
  plus percentage delta vs league median. + = good, - = bad (ERA/WHIP flipped).
- ALERTS line omitted when there are no runs and no reach signals.
