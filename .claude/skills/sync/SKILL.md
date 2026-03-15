---
name: sync
description: >-
  Sync draft picks from the Google Sheet and show what happened. Reports new
  picks, highlights my team's selections, analyzes position runs, threat
  window, and strategy implications. Triggers when the user says "sync",
  "sync picks", "pull picks", "update draft", "what happened", "catch me up",
  or any variation indicating they want to pull and review recent draft activity.
---

# Sync -- Draft Activity Report

When the user asks to sync or catch up on draft activity, pull picks from the
sheet and deliver a structured report of what changed and what it means.

## Pipeline

Run the following Python code. Objects `d` (Draft), `opt` (Optimizer), and
`config` (LeagueConfig) are already initialized per CLAUDE.md workflow.
`reader` is the DraftSheetReader.

```python
# 1. Sync from sheet
try:
    result = d.sync_from_sheet(reader)
except NameError:
    result = None

if result is None:
    print("Sheet reader not configured. Set up reader first.")
elif not result['applied']:
    print("No new picks since last sync.")
    if result['unmatched']:
        print(f"\nUnmatched: {len(result['unmatched'])} picks couldn't be resolved.")
        for sp, err in result['unmatched']:
            print(f"  - {sp.owner} Rd {sp.round_number}: \"{sp.player_name}\" -- {err}")
else:
    # 2. Show synced picks
    out = [f"## SYNC REPORT  |  {len(result['applied'])} new picks", ""]
    for line in result['applied']:
        out.append(line)
    out.append("")

    # 3. Unmatched warning
    if result['unmatched']:
        out.append(f"**UNMATCHED ({len(result['unmatched'])}):**")
        for sp, err in result['unmatched']:
            out.append(f"  - {sp.owner} Rd {sp.round_number}: \"{sp.player_name}\" -- {err}")
        out.append("")

    # 4. My roster snapshot
    my_roster = d.my_roster_players()
    my_picks = d.my_roster()
    if my_picks:
        out.append("**My Roster:**")
        for dp in my_picks:
            p = d.players.get(dp.player_name)
            pos = '/'.join(p.positions) if p else '?'
            out.append(f"  Rd {dp.round_number}: {dp.player_name} ({pos})")
        out.append("")

    # 5. Draft position context
    status_pick = d.state.current_pick
    status_round = d.state.current_round
    picks_until = d.state.picks_until_mine()
    if picks_until is not None and picks_until == 0:
        out.append("**STATUS: It's your pick!**")
    elif picks_until is not None:
        out.append(f"**STATUS:** Rd {status_round}, Pick {status_pick} | {picks_until} picks until your turn")
    out.append("")

    # 6. Position runs
    runs = d.position_runs()
    if runs:
        run_parts = [f"{pos} ({count})" for pos, count in runs.items()]
        out.append(f"**Position Runs:** {' | '.join(run_parts)}")
        out.append("")

    # 7. Threat analysis -- who picks before me and what they need
    threat = d.threat_window()
    if threat:
        out.append("**Threat Window** (picks before your turn):")
        for t in threat:
            team = t['team_name']
            filled = ', '.join(sorted(t['positions_filled'])) if t['positions_filled'] else 'none'
            out.append(f"  Pick {t['pick_number']}: {team} (has: {filled})")
        out.append("")

    # 8. League-relative dashboard
    if my_roster:
        available_all = d.available()
        rel = opt.league_relative_dashboard(my_roster, available=available_all)
        cats = config.all_categories
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
        out.append("**Dashboard vs Last Season:**")
        out.append(f"  {' | '.join(dash_parts[:5])}")
        out.append(f"  {' | '.join(dash_parts[5:])}")
        if rel['hint']:
            out.append(f"  Hint: {rel['hint']}")
        out.append("")

    # 9. Strategy implications
    # Identify what positions opponents are loading up on
    if result['applied']:
        from collections import Counter
        pos_counter = Counter()
        my_new = []
        for line in result['applied']:
            if line.strip().startswith("SKIP"):
                continue
            # Extract positions from the sync line format: "Owner takes Name (POS)"
            if "(" in line and ")" in line:
                pos_str = line[line.rfind("(")+1:line.rfind(")")]
                positions = [p.strip() for p in pos_str.split("/")]
                for pos in positions:
                    pos_counter[pos] += 1

        if pos_counter:
            top_pos = pos_counter.most_common(3)
            trends = [f"{pos} ({count}x)" for pos, count in top_pos]
            out.append(f"**Trends in New Picks:** {', '.join(trends)}")
            out.append("")

    out.append("> Say \"it's my pick\" when ready, or ask about a specific player/team.")
    print("\n".join(out))
```

## Output Rules

1. Print the formatted output exactly as produced. Do not add extra commentary.

2. If `result` is `None` or has no applied picks, just show the short message.

3. After showing the report, **wait for the user's next action**. Do not
   auto-trigger recommendations. The user may want to think, ask questions,
   or say "it's my pick" when ready.

4. If the user asks about a specific team (e.g., "what did Sam pick?"), show
   that team's full roster from `d.team_roster("Sam")`.

5. If unmatched picks exist, remind the user to resolve them with
   `d.pick("Name", "Owner")`.
