#!/usr/bin/env python3
"""Convert prima.cpp per-token JSONL timing into EdgeVisor-shaped reports.

The converter intentionally uses only the Python standard library.  prima.cpp
does not expose pipeline-stage or migration records, so those fields remain
empty instead of being inferred from token timing.
"""

import argparse
import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import sys


SCHEMA_VERSION = 1
CSV_COLUMNS = [
    "pos",
    "rel_pos",
    "phase",
    "token_index",
    "token_id",
    "piece",
    "e2e_wall_ms",
    "elapsed_ms",
    "itl_ms",
    "stage_total_ms",
    "stage_exec_ms",
    "stage_sync_ms",
    "stage_bubble_ms",
    "bubble_drain_us",
    "bubble_elapsed_us",
    "bubble_completed",
    "events",
]


class InputError(Exception):
    """An error that can be reported directly to a command-line user."""


def _number(value, field, line_number):
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise InputError(
            "line %d: field %r must be numeric" % (line_number, field)
        ) from exc


def _integer(value, field, line_number):
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise InputError(
            "line %d: field %r must be an integer" % (line_number, field)
        ) from exc


def load_jsonl(input_path):
    """Load token events and the summary from a prima.cpp JSONL file."""
    tokens = []
    summary = None
    warnings = []

    try:
        stream = open(input_path, "r", encoding="utf-8", errors="replace")
    except OSError as exc:
        raise InputError("cannot open %s: %s" % (input_path, exc)) from exc

    with stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                warnings.append("line %d: ignored malformed JSON (%s)" %
                                (line_number, exc.msg))
                continue

            if not isinstance(event, dict):
                warnings.append("line %d: ignored non-object JSON value" % line_number)
                continue

            event_type = event.get("type")
            if event_type == "token":
                required = ("index", "id", "piece", "elapsed_ms", "itl_ms")
                missing = [field for field in required if field not in event]
                if missing:
                    warnings.append("line %d: ignored token missing %s" %
                                    (line_number, ", ".join(missing)))
                    continue
                tokens.append({
                    "index": _integer(event["index"], "index", line_number),
                    "id": _integer(event["id"], "id", line_number),
                    "piece": str(event["piece"]),
                    "elapsed_ms": _number(event["elapsed_ms"], "elapsed_ms", line_number),
                    "itl_ms": _number(event["itl_ms"], "itl_ms", line_number),
                })
            elif event_type == "summary":
                summary = dict(event)

    if not tokens:
        raise InputError("input contains no valid token events")
    if summary is None:
        raise InputError("input contains no summary event")

    tokens.sort(key=lambda token: token["index"])
    expected = tokens[0]["index"]
    for token in tokens:
        if token["index"] != expected:
            warnings.append("non-contiguous token indexes near %d" % token["index"])
        expected = token["index"] + 1

    return tokens, summary, warnings


def _mean(values):
    return sum(values) / len(values) if values else None


def _rounded(value):
    return None if value is None else round(value, 3)


def detect_onset(tokens, jump_pct):
    """Return the token-list index of an EdgeVisor-style three-token onset."""
    walls = [token["itl_ms"] for token in tokens]
    window = 3
    jump = 1.0 + jump_pct / 100.0
    if len(walls) < window * 2:
        return None

    onset = None
    for index in range(len(walls) - window, window - 1, -1):
        recent = _mean(walls[index:index + window])
        baseline = _mean(walls[index - window:index])
        if baseline and recent > baseline * jump:
            onset = index
        elif onset is not None:
            break
    return onset


def detect_recovery(tokens, onset_index, drop_pct, confirm):
    """Return (effective index, peak, threshold) or (None, peak, threshold)."""
    ratio = 1.0 - drop_pct / 100.0
    peak = 0.0
    run = []
    threshold = None

    for index in range(onset_index, len(tokens)):
        wall = tokens[index]["itl_ms"]
        peak = max(peak, wall)
        threshold = peak * ratio
        if index <= onset_index:
            run = []
        elif wall <= threshold:
            run.append(index)
            if len(run) >= confirm:
                return run[0], peak, threshold
        else:
            run = []

    return None, peak, threshold


def _phase_statistics(rows):
    result = {}
    for phase in ("baseline", "degraded", "recovered"):
        values = [row["e2e_wall_ms"] for row in rows if row["phase"] == phase]
        if values:
            result[phase] = {
                "tokens": len(values),
                "mean_itl_ms": _rounded(_mean(values)),
                "min_itl_ms": _rounded(min(values)),
                "max_itl_ms": _rounded(max(values)),
            }
        else:
            result[phase] = {
                "tokens": 0,
                "mean_itl_ms": None,
                "min_itl_ms": None,
                "max_itl_ms": None,
            }
    return result


def _tpot_comparison(phase_statistics):
    baseline = phase_statistics["baseline"]["mean_itl_ms"]
    degraded = phase_statistics["degraded"]["mean_itl_ms"]
    recovered = phase_statistics["recovered"]["mean_itl_ms"]
    delta = None if baseline is None or degraded is None else degraded - baseline
    increase = None
    if baseline not in (None, 0) and delta is not None:
        increase = delta / baseline * 100.0
    return {
        "baseline_itl_ms": baseline,
        "degraded_itl_ms": degraded,
        "recovered_itl_ms": recovered,
        "degraded_delta_ms": _rounded(delta),
        "degraded_increase_pct": _rounded(increase),
    }


def _parse_meta(values):
    metadata = {}
    for value in values:
        if "=" not in value:
            raise InputError("--meta expects KEY=VALUE, got %r" % value)
        key, item = value.split("=", 1)
        if not key:
            raise InputError("--meta key cannot be empty")
        metadata[key] = item
    return metadata


def convert_file(input_path, args):
    tokens, input_summary, warnings = load_jsonl(input_path)

    if args.baseline_tokens < 0:
        raise InputError("--baseline-tokens must be non-negative")
    if args.recovery_confirm < 1:
        raise InputError("--recovery-confirm must be at least 1")
    if args.tokens_after < 1:
        raise InputError("--tokens-after must be at least 1")
    if args.jump_pct < 0 or args.recovery_drop_pct < 0:
        raise InputError("jump and recovery percentages must be non-negative")

    if args.onset_index is not None:
        if args.onset_index < 0 or args.onset_index >= len(tokens):
            raise InputError("--onset-index is outside the token range")
        onset_index = args.onset_index
        onset_source = "manual(--onset-index)"
    else:
        onset_index = detect_onset(tokens, args.jump_pct)
        if onset_index is None:
            onset_index = 0
            onset_source = "fallback(first-token)"
            warnings.append("no ITL jump detected; using first token as onset")
        else:
            onset_source = "auto(itl-jump=%g%%)" % args.jump_pct

    recovery_index, peak, threshold = detect_recovery(
        tokens, onset_index, args.recovery_drop_pct, args.recovery_confirm
    )
    if recovery_index is None:
        end_index = len(tokens) - 1
        end_source = "log-end(no-recovery)"
        warnings.append("no recovery confirmed; retaining tokens through log end")
    else:
        end_index = min(len(tokens) - 1, recovery_index + args.tokens_after - 1)
        end_source = "recovery+%d(first-effective-counted-as-1)" % args.tokens_after

    baseline_start_index = max(0, onset_index - args.baseline_tokens)
    prompt_tokens = int(input_summary.get("prompt_tokens", 0))
    onset_pos = prompt_tokens + tokens[onset_index]["index"]
    effective_pos = None if recovery_index is None else prompt_tokens + tokens[recovery_index]["index"]

    rows = []
    for index in range(baseline_start_index, end_index + 1):
        token = tokens[index]
        if index < onset_index:
            phase = "baseline"
        elif recovery_index is not None and index >= recovery_index:
            phase = "recovered"
        else:
            phase = "degraded"

        events = []
        if index == onset_index:
            events.append("onset")
        if recovery_index is not None and index == recovery_index:
            events.append("recovery")
        pos = prompt_tokens + token["index"]
        rows.append({
            "pos": pos,
            "rel_pos": pos - onset_pos,
            "phase": phase,
            "token_index": token["index"],
            "token_id": token["id"],
            "piece": token["piece"],
            "e2e_wall_ms": token["itl_ms"],
            "elapsed_ms": token["elapsed_ms"],
            "itl_ms": token["itl_ms"],
            "stages": [],
            "bubble_shadow": None,
            "events": events,
        })

    statistics = _phase_statistics(rows)
    comparison = _tpot_comparison(statistics)
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    meta = {"model": args.model, "ratios": args.ratios}
    meta.update(_parse_meta(args.meta))
    result = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "experiment": args.name,
        "meta": meta,
        "source": str(Path(input_path).resolve()),
        "params": {
            "baseline_tokens": args.baseline_tokens,
            "jump_pct": args.jump_pct,
            "recovery_drop_pct": args.recovery_drop_pct,
            "recovery_confirm": args.recovery_confirm,
            "tokens_after": args.tokens_after,
        },
        "warnings": warnings,
        "window": {
            "onset_pos": onset_pos,
            "onset_source": onset_source,
            "system_tpot_onset": None,
            "baseline_start": prompt_tokens + tokens[baseline_start_index]["index"],
            "baseline_tokens": args.baseline_tokens,
            "baseline_tokens_recorded": statistics["baseline"]["tokens"],
            "end_pos": rows[-1]["pos"],
            "end_source": end_source,
            "recovery": {
                "confirmed": recovery_index is not None,
                "effective_pos": effective_pos,
                "peak_wall_ms": _rounded(peak),
                "threshold_ms": _rounded(threshold),
                "drop_pct": args.recovery_drop_pct,
                "confirm_tokens": args.recovery_confirm,
            },
            "tokens_recorded": len(rows),
        },
        "migrations": [],
        "rejected_commands": [],
        "columns": {"nodes": [], "node_stage": {}},
        "tokens": rows,
        "summary": {
            "sections": {},
            "stage_node_profile": [],
            "migration_tpot_summary": None,
            "token_timing": dict(input_summary),
            "phase_statistics": statistics,
            "tpot_comparison": comparison,
        },
    }
    return result, rows


def _csv_value(row, field):
    if field in ("stage_total_ms", "stage_exec_ms", "stage_sync_ms", "stage_bubble_ms",
                 "bubble_drain_us", "bubble_elapsed_us", "bubble_completed"):
        return ""
    if field == "events":
        return ";".join(row["events"])
    if field == "e2e_wall_ms":
        return "%.2f" % row[field]
    if field in ("elapsed_ms", "itl_ms"):
        return "%.3f" % row[field]
    return row.get(field, "")


def write_outputs(result, rows, outdir, name):
    output_dir = Path(outdir)
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = output_dir / ("%s_%s.json" % (name, timestamp))
    csv_path = output_dir / ("%s_%s_tokens.csv" % (name, timestamp))

    with json_path.open("w", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    with csv_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: _csv_value(row, field) for field in CSV_COLUMNS})
    return json_path, csv_path


def build_parser():
    parser = argparse.ArgumentParser(
        description="Convert prima.cpp per-token timing JSONL to JSON and CSV reports."
    )
    parser.add_argument("input", help="JSONL written by --token-timing-file")
    parser.add_argument("--name", default="token_timing", help="experiment/output name")
    parser.add_argument("--outdir", default=".", help="output directory")
    parser.add_argument("--model", default="unknown")
    parser.add_argument("--ratios", default="")
    parser.add_argument("--meta", action="append", default=[], metavar="KEY=VALUE")
    parser.add_argument("--onset-index", type=int, default=None,
                        help="override automatic onset with generated-token index")
    parser.add_argument("--baseline-tokens", type=int, default=20)
    parser.add_argument("--jump-pct", type=float, default=15.0)
    parser.add_argument("--recovery-drop-pct", type=float, default=15.0)
    parser.add_argument("--recovery-confirm", type=int, default=2)
    parser.add_argument("--tokens-after", type=int, default=16)
    parser.add_argument("--quiet", action="store_true")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        result, rows = convert_file(args.input, args)
        json_path, csv_path = write_outputs(result, rows, args.outdir, args.name)
    except (InputError, OSError, ValueError) as exc:
        print("[converter] ERROR: %s" % exc, file=sys.stderr)
        return 2

    if not args.quiet:
        print("[converter] JSON: %s" % json_path)
        print("[converter] CSV:  %s" % csv_path)
        print("[converter] tokens=%d onset=%s recovery=%s" % (
            len(rows), result["window"]["onset_pos"],
            result["window"]["recovery"]["effective_pos"],
        ))
    return 0


if __name__ == "__main__":
    sys.exit(main())
