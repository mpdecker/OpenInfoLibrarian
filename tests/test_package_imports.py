"""Test top-level package API exports."""

import documentcrawler as dc


def test_top_level_exports():
    assert hasattr(dc, "Config")
    assert hasattr(dc, "Database")
    assert hasattr(dc, "DocumentQuery")
    assert hasattr(dc, "DocumentRow")
    assert hasattr(dc, "Fetcher")
    assert hasattr(dc, "Pipeline")
    assert dc.__version__ == "0.2.0"
