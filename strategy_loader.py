from __future__ import annotations

import importlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Type

from base_strategy import BaseStrategy


@dataclass(frozen=True)
class StrategySpec:
    name: str
    module: str
    class_name: str
    enabled: bool
    symbols: List[str]
    timeframes: List[str]
    config: Dict[str, Any]


def load_engine_config(config_path: str = "strategy_engine_config.json") -> Dict[str, Any]:
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Strategy engine config not found: {config_path}")
    with path.open("r", encoding="utf-8") as file_obj:
        return json.load(file_obj)


def strategy_specs(engine_config: Dict[str, Any]) -> List[StrategySpec]:
    # Try to load live screener symbols from settings.json if running in live_sandbox
    mode = str(engine_config.get("mode", "paper")).lower()
    live_symbols = []
    if mode == "live_sandbox":
        try:
            settings_path = Path("settings.json")
            if settings_path.exists():
                with settings_path.open("r", encoding="utf-8") as f:
                    settings = json.load(f)
                    live_symbols = settings.get("MTF_SCREENER_SYMBOLS", [])
        except Exception:
            pass

    specs: List[StrategySpec] = []
    for item in engine_config.get("strategies", []):
        symbols = list(item.get("symbols", []))
        if mode == "live_sandbox" and live_symbols:
            symbols = list(live_symbols)

        specs.append(
            StrategySpec(
                name=item["name"],
                module=item["module"],
                class_name=item["class"],
                enabled=bool(item.get("enabled", True)),
                symbols=symbols,
                timeframes=list(item.get("timeframes", [])),
                config=dict(item.get("config", {})),
            )
        )
    return specs


def import_strategy_class(spec: StrategySpec) -> Type[BaseStrategy]:
    module = importlib.import_module(spec.module)
    strategy_class = getattr(module, spec.class_name)
    if not issubclass(strategy_class, BaseStrategy):
        raise TypeError(f"{spec.module}.{spec.class_name} must inherit BaseStrategy")
    return strategy_class
