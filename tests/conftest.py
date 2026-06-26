import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from documentcrawler.config import Config  # noqa: E402
from documentcrawler.db import Database  # noqa: E402


@pytest.fixture
def sample_config() -> Config:
    return Config()


@pytest.fixture
def tmp_db(tmp_path):
    return Database(tmp_path / "test.db")
