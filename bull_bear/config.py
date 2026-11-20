"""牛熊评分算法配置：指数清单、权重、窗口、阈值均来自 JSON 文件。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_DIR / "config" / "bull_bear.json"

_DEFAULT_INDICES = [
    {"code": "000016", "name": "上证50", "prefix": "sh", "group": "large_value"},
    {"code": "000300", "name": "沪深300", "prefix": "sh", "group": "large_value"},
    {"code": "000905", "name": "中证500", "prefix": "sh", "group": "mid500"},
    {"code": "399006", "name": "创业板指", "prefix": "sz", "group": "growth"},
    {"code": "000688", "name": "科创50", "prefix": "sh", "group": "growth"},
]


@dataclass
class RegimeConfig:
    indices: list[dict[str, str]] = field(default_factory=lambda: [dict(item) for item in _DEFAULT_INDICES])
    weights: dict[str, float] = field(default_factory=lambda: {"f1": 0.5, "f2": 0.3, "f3": 0.2})
    group_weights: dict[str, float] = field(
        default_factory=lambda: {"large_value": 0.4, "mid500": 0.2, "growth": 0.4}
    )
    groups: dict[str, list[str]] = field(
        default_factory=lambda: {
            "large_value": ["000016", "000300"],
            "mid500": ["000905"],
            "growth": ["399006", "000688"],
        }
    )
    windows: dict[str, int] = field(
        default_factory=lambda: {
            "ma_short": 60,
            "ma_long": 120,
            "slope_short": 20,
            "slope_long": 60,
            "percentile": 1250,
        }
    )
    thresholds: dict[str, dict[str, float]] = field(
        default_factory=lambda: {
            "index": {"bear": -20.0, "bull": 20.0},
            "composite": {"bear": -20.0, "bull": 20.0},
            "large_value": {"bear": -20.0, "bull": 20.0},
            "mid500": {"bear": -20.0, "bull": 20.0},
            "growth": {"bear": -20.0, "bull": 20.0},
        }
    )
    breadth_multiplier: float = 0.3
    smoothing: dict[str, Any] = field(
        default_factory=lambda: {"ema_enabled": False, "ema_days": 5, "confirm_days": 0}
    )
    start_date: str = "20160101"
    cache_dir: Path = field(default_factory=lambda: PROJECT_DIR / "data" / "regime")
    history_path: Path = field(default_factory=lambda: PROJECT_DIR / "data" / "regime" / "bull_bear_history.csv")
    latest_path: Path = field(default_factory=lambda: PROJECT_DIR / "data" / "regime" / "bull_bear_latest.json")

    def __post_init__(self) -> None:
        for name in ("cache_dir", "history_path", "latest_path"):
            value = Path(getattr(self, name))
            if not value.is_absolute():
                value = PROJECT_DIR / value
            setattr(self, name, value)
        self._validate()

    def _validate(self) -> None:
        for key, weight in self.weights.items():
            if weight < 0:
                raise ValueError(f"因子权重不能为负：{key}")
        if abs(sum(self.weights.values()) - 1.0) > 1e-6:
            raise ValueError("三因子权重之和必须为 1")
        if abs(sum(self.group_weights.values()) - 1.0) > 1e-6:
            raise ValueError("分组权重之和必须为 1")
        codes = [item["code"] for item in self.indices]
        if len(set(codes)) != len(self.indices):
            raise ValueError("指数代码重复")
        grouped = [code for members in self.groups.values() for code in members]
        if set(grouped) != set(codes):
            raise ValueError("分组指数与指数清单不一致")
        for name, bounds in self.thresholds.items():
            if float(bounds["bear"]) >= float(bounds["bull"]):
                raise ValueError(f"阈值 bear 必须小于 bull：{name}")

    @property
    def codes(self) -> list[str]:
        return [item["code"] for item in self.indices]

    def name_of(self, code: str) -> str:
        for item in self.indices:
            if item["code"] == code:
                return str(item["name"])
        return code

    def to_dict(self) -> dict[str, Any]:
        return {
            "indices": [dict(item) for item in self.indices],
            "weights": dict(self.weights),
            "group_weights": dict(self.group_weights),
            "groups": {key: list(value) for key, value in self.groups.items()},
            "windows": dict(self.windows),
            "thresholds": {key: dict(value) for key, value in self.thresholds.items()},
            "breadth_multiplier": self.breadth_multiplier,
            "smoothing": dict(self.smoothing),
            "start_date": self.start_date,
            "cache_dir": str(self.cache_dir.relative_to(PROJECT_DIR)),
            "history_path": str(self.history_path.relative_to(PROJECT_DIR)),
            "latest_path": str(self.latest_path.relative_to(PROJECT_DIR)),
        }

    def save(self, path: str | Path = DEFAULT_CONFIG_PATH) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")


def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> RegimeConfig:
    """读取配置；文件不存在时写出默认配置并返回。"""
    source = Path(path)
    if not source.exists():
        config = RegimeConfig()
        config.save(source)
        return config
    payload = json.loads(source.read_text(encoding="utf-8"))
    defaults = RegimeConfig().to_dict()
    defaults.update(payload)
    return RegimeConfig(**defaults)
