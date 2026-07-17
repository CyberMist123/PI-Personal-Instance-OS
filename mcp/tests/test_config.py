from pathlib import Path

from cmx_mcp.config import InstanceSettings, Paths


def test_loads_web_domain_from_env_production(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "mcp"
    home.mkdir()
    (tmp_path / ".env.production").write_text("WEB_DOMAIN=pi.example.test\n", encoding="utf-8")
    monkeypatch.delenv("CMX_MASTODON_HOST", raising=False)
    monkeypatch.delenv("WEB_DOMAIN", raising=False)
    monkeypatch.delenv("CMX_MASTODON_BASE_URL", raising=False)

    paths = Paths(
        home=home,
        runtime=home / "runtime",
        database=home / "runtime" / "cmx.sqlite3",
        secrets=home / "runtime" / "secrets",
        logs=home / "runtime" / "logs",
    )
    settings = InstanceSettings.load(paths)

    assert settings.host_header == "pi.example.test"
    assert settings.public_base_url == "https://pi.example.test"
