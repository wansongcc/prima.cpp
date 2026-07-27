# Per-token Timing Experiment Converter

## Goal

Add a standard-library-only Python script under `prima.cpp/scripts/` that converts
the JSONL emitted by `--token-timing-file` into EdgeVisor-compatible JSON and
token-level CSV files for visualization and comparison experiments.

## Input

The input contains `type=token` records with `index`, `id`, `piece`,
`elapsed_ms`, and `itl_ms`, followed by one `type=summary` record. The converter
uses `itl_ms` as the per-token end-to-end wall-time equivalent (`e2e_wall_ms`),
because it measures the interval between produced tokens. It preserves the
original cumulative timing and token text in every output token row.

## Output compatibility

The JSON top-level layout follows EdgeVisor's recorder:

- `schema_version`, `generated_at`, `experiment`, `meta`, `source`, `params`,
  `warnings`, `window`, `migrations`, `rejected_commands`, `columns`, `tokens`,
  and `summary` are always present.
- `pos` is `prompt_tokens + index`; `rel_pos` is relative to the detected onset.
- `phase` is `baseline`, `degraded`, or `recovered`.
- `stages` is an empty array, `bubble_shadow` is null, and migration arrays are
  empty because the prima.cpp JSONL does not contain those measurements.
- `summary` retains empty EdgeVisor-compatible `sections`,
  `stage_node_profile`, and `migration_tpot_summary`, and adds the original
  token timing summary plus derived phase statistics.

The CSV follows the same per-token orientation as EdgeVisor while adding
`token_index`, `token_id`, `piece`, `elapsed_ms`, and `itl_ms`. Stage, bubble,
and event columns remain present but empty so plotting scripts can share a
schema without treating absent prima.cpp measurements as zero.

## Timing degradation analysis

The converter applies EdgeVisor-style 3-token sliding-window onset detection to
`itl_ms`. `--onset-index` overrides automatic detection. It records a baseline
window before onset, marks tokens after onset as degraded, and marks recovered
tokens after a peak drops by `--recovery-drop-pct` for
`--recovery-confirm` consecutive tokens. If no recovery is found, the output
extends to the final token and records a warning.

## CLI and errors

The CLI accepts `--name`, `--outdir`, `--model`, `--ratios`, repeatable
`--meta KEY=VALUE`, onset/baseline/recovery thresholds, and `--quiet`.
Malformed JSONL lines generate warnings and are skipped; missing token events
or a missing summary are fatal input errors. The script writes UTF-8 pretty
JSON and UTF-8 CSV with empty strings for unavailable measurements.
