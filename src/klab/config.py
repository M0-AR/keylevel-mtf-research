"""Configuration loading and repository paths."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


def repo_root() -> Path:
    """Return the repository root, allowing an override for containers."""
    env = os.environ.get("KLAB_ROOT")
    if env:
        return Path(env).resolve()
    return Path(__file__).resolve().parents[2]


def load_yaml(path: str | Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"expected a mapping in {path}")
    return data


@dataclass(frozen=True)
class Instrument:
    symbol: str
    asset_class: str
    histdata_symbol: str
    yahoo_symbol: str
    pip_size: float
    price_digits: int
    half_spread_price: float
    slippage_price: float

    @property
    def round_trip_cost(self) -> float:
        return 2.0 * (self.half_spread_price + self.slippage_price)


def load_instruments(path: str | Path) -> dict[str, Instrument]:
    raw = load_yaml(path)["instruments"]
    out: dict[str, Instrument] = {}
    for symbol, spec in raw.items():
        out[symbol] = Instrument(
            symbol=symbol,
            asset_class=spec["asset_class"],
            histdata_symbol=spec["histdata_symbol"],
            yahoo_symbol=spec["yahoo_symbol"],
            pip_size=float(spec["pip_size"]),
            price_digits=int(spec["price_digits"]),
            half_spread_price=float(spec["half_spread_price"]),
            slippage_price=float(spec["slippage_price"]),
        )
    return out


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    root = repo_root()
    cfg_path = Path(path) if path else root / "config" / "base.yaml"
    if not cfg_path.is_absolute():
        cfg_path = root / cfg_path
    cfg = load_yaml(cfg_path)
    cfg["_config_path"] = str(cfg_path)
    cfg["_root"] = str(root)
    inst_path = Path(cfg["data"]["instrument_file"])
    if not inst_path.is_absolute():
        inst_path = root / inst_path
    cfg["instruments"] = load_instruments(inst_path)
    return cfg


def resolve(cfg: dict[str, Any], relative: str) -> Path:
    p = Path(relative)
    if p.is_absolute():
        return p
    return Path(cfg["_root"]) / p


def split_dates(cfg: dict[str, Any], split: str) -> tuple[str, str]:
    spec = cfg["splits"][split]
    return str(spec["start"]), str(spec["end"])
