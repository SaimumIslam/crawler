"""Export a run's records to JSON, JSONL, or CSV."""
from __future__ import annotations

import csv
import json
from pathlib import Path

from ..config import EXPORT_DIR, ensure_dirs
from .db import Store


def export_run(
    store: Store, run_id: int, fmt: str = "json", out_path: str | None = None
) -> Path:
    ensure_dirs()
    records = store.records_for_run(run_id)
    fmt = fmt.lower()
    if out_path:
        path = Path(out_path)
    else:
        path = EXPORT_DIR / f"run_{run_id}.{fmt if fmt != 'jsonl' else 'jsonl'}"
    path.parent.mkdir(parents=True, exist_ok=True)

    if fmt == "json":
        path.write_text(
            json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    elif fmt == "jsonl":
        with path.open("w", encoding="utf-8") as fh:
            for rec in records:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    elif fmt == "csv":
        fields: list[str] = []
        for rec in records:
            for k in rec:
                if k not in fields:
                    fields.append(k)
        with path.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=fields)
            writer.writeheader()
            for rec in records:
                writer.writerow(
                    {k: _flat(rec.get(k)) for k in fields}
                )
    else:
        raise ValueError(f"unknown export format: {fmt}")
    return path


def _flat(v: object) -> object:
    if isinstance(v, (list, dict)):
        return json.dumps(v, ensure_ascii=False)
    return v
