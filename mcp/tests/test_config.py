from pathlib import Path

import pytest

from cmx_mcp.config import InstanceSettings, Paths


def _paths(home: Path) -> Paths:
    return Paths(
        home=home,
        runtime=home / "runtime",
        database=home / "runtime" / "cmx.sqlite3",
        secrets=home / "runtime" / "secrets",
        logs=home / "runtime" / "logs",
    )


def test_loads_web_domain_from_env_production_and_defaults_to_https(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "mcp"
    home.mkdir()
    (tmp_path / ".env.production").write_text("WEB_DOMAIN=pi.example.test\n", encoding="utf-8")
    monkeypatch.delenv("CMX_MASTODON_HOST", raising=False)
    monkeypatch.delenv("WEB_DOMAIN", raising=False)
    monkeypatch.delenv("CMX_MASTODON_BASE_URL", raising=False)

    settings = InstanceSettings.load(_paths(home))

    assert settings.host_header == "pi.example.test"
    assert settings.public_base_url == "https://pi.example.test"
    assert settings.base_url == "https://pi.example.test"


def test_allows_explicit_loopback_http(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "mcp"
    home.mkdir()
    (tmp_path / ".env.production").write_text("WEB_DOMAIN=pi.example.test\n", encoding="utf-8")
    monkeypatch.setenv("CMX_MASTODON_BASE_URL", "http://127.0.0.1:8080")

    settings = InstanceSettings.load(_paths(home))

    assert settings.base_url == "http://127.0.0.1:8080"


def test_rejects_unrelated_https_host(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "mcp"
    home.mkdir()
    (tmp_path / ".env.production").write_text("WEB_DOMAIN=pi.example.test\n", encoding="utf-8")
    monkeypatch.setenv("CMX_MASTODON_BASE_URL", "https://evil.example")

    with pytest.raises(RuntimeError, match="must match"):
        InstanceSettings.load(_paths(home))


def test_char_budget_accepts_deprecated_token_alias_and_new_name_wins(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "mcp"; home.mkdir()
    (tmp_path / ".env.production").write_text("WEB_DOMAIN=pi.example.test\n", encoding="utf-8")
    monkeypatch.setenv("CMX_BROWSE_TOKEN_BUDGET", "4000")
    settings = InstanceSettings.load(_paths(home))
    assert settings.browse_char_budget == 4000
    monkeypatch.setenv("CMX_BROWSE_CHAR_BUDGET", "4500")
    settings = InstanceSettings.load(_paths(home))
    assert settings.browse_char_budget == 4500
