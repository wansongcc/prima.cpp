# Per-token Timing Experiment Converter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Convert prima.cpp token timing JSONL into EdgeVisor-compatible JSON and CSV with automatic TPOT degradation/recovery phase labeling.

**Architecture:** Keep parsing and analysis in a standalone Python 3 script using only the standard library. Treat `itl_ms` as `e2e_wall_ms`, preserve all source timing fields, and emit empty stage/migration fields rather than fabricating unavailable data.

**Tech Stack:** Python 3 standard library (`argparse`, `csv`, `datetime`, `json`, `os`, `statistics`, `sys`), unittest-style subprocess tests.

## Global Constraints

- The script lives in `prima.cpp/scripts/`, never in the EdgeVisor repository.
- JSON top-level fields mirror the EdgeVisor recorder schema.
- CSV includes timing columns plus empty stage/bubble/event columns.
- `itl_ms` is the per-token `e2e_wall_ms` signal for onset/recovery analysis.
- Missing stage, migration, and event data is represented by empty arrays/null/empty CSV cells.
- No third-party Python dependency is required.

---

### Task 1: Add a failing converter test

**Files:**
- Create: `tests/test-token-timing-converter.py`

**Interfaces:**
- Consumes: future `scripts/convert_token_timing.py` CLI.
- Produces: assertions for JSON/CSV mapping and degraded/recovered phase detection.

- [ ] **Step 1: Write the failing test**

Create a temporary JSONL input with 20 token events: five 10ms baseline tokens,
three 20ms degraded tokens, then twelve 10ms recovered tokens, followed by a
summary. Run the script with `--baseline-tokens 5 --jump-pct 15
--recovery-drop-pct 15 --recovery-confirm 2` and assert:

```python
assert payload["window"]["onset_source"].startswith("auto(")
assert any(row["phase"] == "degraded" for row in payload["tokens"])
assert any(row["phase"] == "recovered" for row in payload["tokens"])
assert payload["tokens"][0]["stages"] == []
assert payload["tokens"][0]["bubble_shadow"] is None
assert payload["summary"]["token_timing"]["generated_tokens"] == 20
assert csv_rows[0]["stage_exec_ms"] == ""
```

- [ ] **Step 2: Run it to verify it fails**

Run:

```bash
python3 tests/test-token-timing-converter.py
```

Expected: FAIL because `scripts/convert_token_timing.py` does not exist.

### Task 2: Implement parsing, phase analysis, and outputs

**Files:**
- Create: `scripts/convert_token_timing.py`

**Interfaces:**
- Consumes: JSONL path and CLI options.
- Produces: `convert_file(input_path, args) -> (dict, list[dict])`,
  `write_outputs(result, rows, outdir, name) -> (json_path, csv_path)`, and a
  command-line executable.

- [ ] **Step 1: Implement JSONL parsing**

Parse token objects into ordered rows, capture the summary object, validate
required timing keys, and append warnings for malformed lines or non-contiguous
indexes. Raise a user-facing error if no token events or summary are present.

- [ ] **Step 2: Implement onset and recovery analysis**

Use three-token windows over `itl_ms`. Detect the earliest sustained jump where
recent mean exceeds baseline mean by `jump_pct`; use `--onset-index` when given,
otherwise fall back to index 0 with a warning. After onset, find the peak and
mark recovery after `recovery_confirm` consecutive values at or below
`peak * (1 - recovery_drop_pct / 100)`. Assign phase labels and absolute
positions using `prompt_tokens + index`.

- [ ] **Step 3: Build the EdgeVisor-shaped JSON**

Populate the required top-level fields, use empty/null unavailable fields, and
add `summary.token_timing` with the input summary plus counts and mean ITL by
phase. Add event labels only for onset and recovery; do not create migration
events.

- [ ] **Step 4: Write the CSV**

Write columns:

```text
pos,rel_pos,phase,token_index,token_id,piece,e2e_wall_ms,elapsed_ms,itl_ms,stage_total_ms,stage_exec_ms,stage_sync_ms,stage_bubble_ms,bubble_drain_us,bubble_elapsed_us,bubble_completed,events
```

Use empty strings for every unavailable stage/bubble value and semicolon-join
event labels.

- [ ] **Step 5: Add CLI metadata and error handling**

Support `--name`, `--outdir`, `--model`, `--ratios`, repeatable `--meta`,
`--onset-index`, `--baseline-tokens`, `--jump-pct`,
`--recovery-drop-pct`, `--recovery-confirm`, `--tokens-after`, and `--quiet`.
Write `<name>_<timestamp>.json` and `<name>_<timestamp>_tokens.csv`.

### Task 3: Verify and document usage

**Files:**
- Modify: `tests/test-token-timing-converter.py`
- Verify: `scripts/convert_token_timing.py`

- [ ] **Step 1: Run the converter test**

```bash
python3 tests/test-token-timing-converter.py
```

Expected: PASS with JSON and CSV assertions satisfied.

- [ ] **Step 2: Run it against a real token-timing JSONL**

```bash
python3 scripts/convert_token_timing.py /path/to/token-timing.jsonl --outdir /tmp/token-report --name prima_run --quiet
python3 -c 'import json, glob; p=glob.glob("/tmp/token-report/prima_run_*.json")[0]; d=json.load(open(p)); assert d["tokens"]; assert d["summary"]["token_timing"]'
```

Expected: one JSON and one CSV file with equal token-row counts and valid JSON.

- [ ] **Step 3: Run whitespace and repository status checks**

```bash
git diff --check
git status --short --branch
```

Expected: no whitespace errors and only the intended prima.cpp files changed.
