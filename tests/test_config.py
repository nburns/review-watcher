import pytest
from pydantic import ValidationError

from review_watcher.config import Config


def test_empty_repos_raises() -> None:
    with pytest.raises(ValidationError):
        Config.model_validate({"github_login": "octocat", "repos": []})


def test_allowlist_matches_github_canonical_case() -> None:
    config = Config.model_validate({"github_login": "octocat", "repos": ["nburns/review-watcher"]})

    assert config.allows_repo("nburns/review-watcher")
    assert config.allows_repo("NBurns/Review-Watcher")
    assert not config.allows_repo("nburns/other")


def test_repos_accept_plain_names_and_tables_with_instructions() -> None:
    config = Config.model_validate(
        {
            "github_login": "octocat",
            "repos": [
                "acme/plain",
                {"name": "Acme/App", "instructions": ["  Check the migrations.  "]},
            ],
        }
    )

    assert config.repo_names == ["acme/plain", "acme/app"]
    assert config.instructions_for("ACME/app") == ["Check the migrations."]
    assert config.instructions_for("acme/plain") == []
    with pytest.raises(ValidationError, match="more than once"):
        Config.model_validate(
            {"github_login": "octocat", "repos": ["acme/app", {"name": "ACME/app"}]}
        )


@pytest.mark.parametrize(
    "auth",
    [
        {"name": "../x", "check": ["true"], "hint": "h"},
        {"name": "a/b", "check": ["true"], "hint": "h"},
        {"name": "gcloud", "check": ["true"], "hint": "h", "login": []},
    ],
)
def test_auth_rejects_path_like_names_and_empty_login(auth: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Config.model_validate({"github_login": "octocat", "repos": ["acme/app"], "auth": [auth]})


def test_context_sources_must_reference_configured_auth() -> None:
    with pytest.raises(ValidationError, match="unknown auth: gcloud"):
        Config.model_validate(
            {
                "github_login": "octocat",
                "repos": [{"name": "acme/app", "context": ["logs"]}],
                "context": {
                    "logs": {"kind": "gcloud_logs", "auth": "gcloud", "project": "p", "filter": "f"}
                },
            }
        )
