import pytest

from documentcrawler.config import (
    Config,
    write_default_config,
)
from documentcrawler.errors import ConfigError


def test_default_config_validates_without_errors():
    cfg = Config()
    warnings = cfg.validate()
    assert isinstance(warnings, list)


def test_missing_download_dir_warns(tmp_path):
    cfg = Config()
    file_as_dir = tmp_path / "file.txt"
    file_as_dir.write_text("not a directory")
    cfg.general.download_dir = file_as_dir / "child"
    with pytest.raises(ConfigError):
        cfg.validate()


def test_invalid_workers_warns():
    cfg = Config()
    cfg.general.workers = 0
    warnings = cfg.validate()
    assert any("workers" in w for w in warnings)

    cfg.general.workers = 20
    warnings = cfg.validate()
    assert any("workers" in w for w in warnings)


def test_invalid_request_timeout_warns():
    cfg = Config()
    cfg.general.request_timeout_s = 0
    warnings = cfg.validate()
    assert any("request_timeout_s" in w for w in warnings)


def test_invalid_max_retries_warns():
    cfg = Config()
    cfg.general.max_retries = 20
    warnings = cfg.validate()
    assert any("max_retries" in w for w in warnings)


def test_invalid_min_pdf_bytes_warns():
    cfg = Config()
    cfg.general.min_pdf_bytes = 1
    warnings = cfg.validate()
    assert any("min_pdf_bytes" in w for w in warnings)


def test_filename_template_needs_placeholder():
    cfg = Config()
    cfg.general.filename_template = "no_placeholder_here"
    with pytest.raises(ConfigError, match="placeholder"):
        cfg.validate()


def test_unknown_source_in_order_warns():
    cfg = Config()
    cfg.sources_order = ["nonexistent_source"]
    warnings = cfg.validate()
    assert any("nonexistent_source" in w for w in warnings)


def test_valid_config_no_warnings(tmp_path):
    dl = tmp_path / "downloads"
    dl.mkdir()
    cfg = Config()
    cfg.general.download_dir = dl
    cfg.general.db_path = tmp_path / "test.db"
    warnings = cfg.validate()
    assert not any("workers" in w for w in warnings)
    assert not any("request_timeout" in w for w in warnings)


def test_config_reload_fresh_copy():
    cfg1 = Config()
    cfg2 = Config.reload(None)
    assert cfg1.general.workers == cfg2.general.workers


def test_write_default_config_uses_example(tmp_path, monkeypatch):
    import documentcrawler.config as config_module

    example = tmp_path / "config.example.toml"
    example_content = ('[general]\ndownload_dir = "./downloads"\ndb_path = "./crawler.db"\n'
                       'filename_template = "{first_author_last}_{year}_{title_slug}.{ext}"\n'
                       'workers = 4\n')
    example.write_text(example_content, encoding="utf-8")
    monkeypatch.setattr(config_module, "EXAMPLE_CONFIG_PATH", example)

    target = tmp_path / "config.toml"
    result = write_default_config(target)
    assert result == target
    assert target.exists()
    content = target.read_text(encoding="utf-8")
    assert "download_dir" in content


def test_write_default_config_fallback(tmp_path, monkeypatch):
    import documentcrawler.config as config_module

    fake = tmp_path / "nonexistent_example.toml"
    monkeypatch.setattr(config_module, "EXAMPLE_CONFIG_PATH", fake)

    target = tmp_path / "config.toml"
    result = write_default_config(target)
    assert result == target
    assert target.exists()


def test_fallback_config_shadow_disabled():
    from documentcrawler.config import _FALLBACK_CONFIG
    assert "[sources.scihub]" in _FALLBACK_CONFIG
    assert "enabled = false" in _FALLBACK_CONFIG
    assert "[sources.zlibrary]" in _FALLBACK_CONFIG
