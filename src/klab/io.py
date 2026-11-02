"""Filesystem, hashing and provenance helpers."""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import pandas as pd


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ensure_dir(path: str | Path) -> Path:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def sha256_file(path: str | Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def hash_files(paths: Iterable[str | Path]) -> str:
    h = hashlib.sha256()
    for p in sorted(Path(x) for x in paths):
        if not p.exists():
            continue
        h.update(str(p).encode("utf-8"))
        h.update(b"\0")
        h.update(sha256_file(p).encode("ascii"))
        h.update(b"\0")
    return h.hexdigest()


def write_json(path: str | Path, obj: Any) -> None:
    p = Path(path)
    ensure_dir(p.parent)
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, sort_keys=True, default=str)
        fh.write("\n")


def read_json(path: str | Path) -> Any:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def write_csv(path: str | Path, df: pd.DataFrame) -> None:
    p = Path(path)
    ensure_dir(p.parent)
    df.to_csv(p, index=False)


def atomic_parquet(path: str | Path, df: pd.DataFrame) -> None:
    p = Path(path)
    ensure_dir(p.parent)
    tmp = p.with_suffix(p.suffix + ".tmp")
    df.to_parquet(tmp, index=True)
    os.replace(tmp, p)


def read_parquet(path: str | Path) -> pd.DataFrame:
    return pd.read_parquet(path)
