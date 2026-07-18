from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
OVERRIDE_PATH = (
    REPOSITORY_ROOT
    / "mastodon-overrides"
    / "v4.6.3"
    / "app"
    / "validators"
    / "status_length_validator.rb"
)
MOUNT = (
    "./mastodon-overrides/v4.6.3/app/validators/status_length_validator.rb:"
    "/opt/mastodon/app/validators/status_length_validator.rb:ro"
)


def test_mastodon_override_sets_5000_character_limit() -> None:
    override = OVERRIDE_PATH.read_text(encoding="utf-8")

    assert "MAX_CHARS = 5000" in override
    assert "each_grapheme_cluster.size" in override
    assert "URL_PLACEHOLDER_CHARS = 23" in override


def test_compose_mounts_override_into_web_and_sidekiq() -> None:
    compose = (REPOSITORY_ROOT / "compose.yml").read_text(encoding="utf-8")

    assert compose.count(MOUNT) == 2
