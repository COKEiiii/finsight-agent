from datetime import date

import pytest

from finsight.data.demo import make_demo
from finsight.data.quality import audit
from finsight.data.store import Store
from finsight.models import StrategyConfig


@pytest.fixture
def bundle():
    value = make_demo(date(2025, 2, 28), date(2024, 1, 2))
    assert audit(value)["publishable"]
    return value


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "workspace")


@pytest.fixture
def config():
    return StrategyConfig()
