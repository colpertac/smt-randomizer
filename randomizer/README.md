# Randomizer gear stars

Star ranking (1–5) for every droppable gear piece on the randomizer server.

| File | Who edits | What |
| --- | --- | --- |
| `stat-tiers.csv` | you | One row per stat. Edit `tier` (1 best … 6 minor, 0 ignore) and `lower_is_better`. Other columns are refreshed on every run. |
| `gear-overrides.csv` | you | `id,star,note` — force an item's star, or `star` 0 to never drop it. Debug/GM items (`[Debug Use]`, Debug Machete, Trainee gear, Unimplemented) are excluded automatically by `EXCLUDE_RX` in the script. |
| `out/gear-stars.csv` | generated | Review sheet: every item, its star, score, and top stats. |
| `out/gear-stars.json` | generated | Star → item IDs, input for the channel drop hook. |

Regenerate after any edit:

```bash
python3 randomizer/rank_gear.py
```

Tier weights: 1 = 10, 2 = 6, 3 = 4, 4 = 2.5, 5 = 1.5, 6 = 0.5. Each stat is
scaled by its typical (90th percentile) value, so +20 FCC and +3000 LB cap are
comparable. Conditional set bonuses count half, partner-demon bonuses about a
third. Stars are cut per slot: bottom 40% 1★, next 25% 2★, 18% 3★, 12% 4★,
top 5% 5★. Tune these constants at the top of `rank_gear.py`.
