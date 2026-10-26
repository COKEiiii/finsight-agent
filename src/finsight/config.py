import json
from pathlib import Path

from finsight.models import StrategyConfig


def load_strategy(path: str | Path | None = None) -> StrategyConfig:
    if path is None:
        return StrategyConfig()
    return StrategyConfig.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))
