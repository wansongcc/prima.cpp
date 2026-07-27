#!/usr/bin/env python3

import csv
import json
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "convert_token_timing.py"


def main():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        input_path = tmp_path / "timing.jsonl"
        output_dir = tmp_path / "output"

        walls = [10.0] * 5 + [20.0] * 3 + [10.0] * 12
        lines = []
        elapsed = 0.0
        for index, itl_ms in enumerate(walls):
            elapsed += itl_ms
            lines.append(json.dumps({
                "type": "token",
                "index": index,
                "id": 1000 + index,
                "piece": " piece-%d" % index,
                "elapsed_ms": elapsed,
                "itl_ms": itl_ms,
            }))
        lines.append(json.dumps({
            "type": "summary",
            "prompt_tokens": 3,
            "generated_tokens": len(walls),
            "ttft_ms": walls[0],
            "total_generation_ms": elapsed,
            "average_itl_ms": elapsed / len(walls),
        }))
        input_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        subprocess.run([
            sys.executable,
            str(SCRIPT),
            str(input_path),
            "--outdir", str(output_dir),
            "--name", "synthetic",
            "--baseline-tokens", "5",
            "--jump-pct", "15",
            "--recovery-drop-pct", "15",
            "--recovery-confirm", "2",
            "--quiet",
        ], check=True)

        json_path = next(output_dir.glob("synthetic_*.json"))
        csv_path = next(output_dir.glob("synthetic_*_tokens.csv"))
        payload = json.loads(json_path.read_text(encoding="utf-8"))
        with csv_path.open(encoding="utf-8", newline="") as stream:
            csv_rows = list(csv.DictReader(stream))

        assert payload["window"]["onset_source"].startswith("auto(")
        assert payload["window"]["recovery"]["confirmed"] is True
        assert any(row["phase"] == "degraded" for row in payload["tokens"])
        assert any(row["phase"] == "recovered" for row in payload["tokens"])
        assert payload["tokens"][0]["stages"] == []
        assert payload["tokens"][0]["bubble_shadow"] is None
        assert payload["summary"]["token_timing"]["generated_tokens"] == 20
        assert len(csv_rows) == 20
        assert csv_rows[0]["stage_exec_ms"] == ""
        assert csv_rows[0]["e2e_wall_ms"] == "10.00"

    print("test-token-timing-converter: PASS")


if __name__ == "__main__":
    main()
