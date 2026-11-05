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
