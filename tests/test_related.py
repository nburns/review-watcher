from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from review_watcher.auth import AuthChecker
from review_watcher.config import Config, ReviewerConfig
from review_watcher.github import PullRequestWithState, RepositoryInfo
from review_watcher.pipeline import Action, Pipeline
from review_watcher.related import find_references
from review_watcher.reviewer import LockdownViolation, assert_lockdown, build_claude_argv
from review_watcher.state import StateStore
from tests.fakes import (
    GIT,
    FakeContextFetcher,
    FakeGitHub,
    FakeNotifier,
    FakeRepoCache,
    FakeReviewer,
    LocalRepo,
    commit_files,
    diff_between,
    empty_draft,
    git,
    make_config,
    make_local_repo,
    make_pr,
    publish,
)

REPO_INFO: dict[str, object] = {
    "acme/app": {"description": "The app."},
    "Acme/Lib": {"description": "Shared contracts."},
}
RELATED: dict[str, object] = {
    "repos": ["acme/app"],
    "repo_info": REPO_INFO,
    "related": [{"repos": ["acme/app", "acme/lib"]}],
}


@dataclass
class Clock:
    now: datetime

    def __call__(self) -> datetime:
        return self.now


def test_related_groups_are_symmetric_and_need_descriptions() -> None:
    config = make_config(
        repos=["acme/app"],
        repo_info={**REPO_INFO, "acme/other": {"description": "Other."}},
        related=[{"repos": ["acme/app", "acme/lib"]}, {"repos": ["acme/lib", "acme/other"]}],
    )

    assert config.related_for("ACME/app") == ["acme/lib"]
    assert config.related_for("acme/lib") == ["acme/app", "acme/other"]
    assert config.related_for("acme/other") == ["acme/lib"]
    assert config.related_for("acme/unrelated") == []
    with pytest.raises(ValidationError, match="repo_info description: acme/lib"):
        Config.model_validate(
            {
                "github_login": "octocat",
                "repos": ["acme/app"],
                "repo_info": {"acme/app": {"description": "The app."}},
                "related": [{"repos": ["acme/app", "acme/lib"]}],
            }
        )


def test_references_outside_the_related_set_are_ignored() -> None:
    body = (
        "Deploy after Acme/Lib#471 is live. See https://github.com/acme/lib/pull/471 and "
        "https://github.com/evil/repo/pull/1, also acme/lib#9 and path/acme/lib#3."
    )

    scan = find_references(body, ["acme/lib"])

    assert [ref.label for ref in scan.allowed] == ["acme/lib#471", "acme/lib#9"]
    assert [ref.label for ref in scan.ignored] == ["evil/repo#1"]


def test_argv_grants_read_access_to_related_dirs_only_by_absolute_path() -> None:
    argv = build_claude_argv(
        binary=Path("/bin/claude"),
        config=ReviewerConfig(),
        schema_json="{}",
        system_prompt="rubric",
        read_dirs=[Path("/var/related/acme/lib")],
    )

    assert_lockdown(argv)
    assert argv[argv.index("--add-dir") + 1] == "/var/related/acme/lib"
    with pytest.raises(LockdownViolation, match="absolute"):
        build_claude_argv(
            binary=Path("/bin/claude"),
            config=ReviewerConfig(),
            schema_json="{}",
            system_prompt="rubric",
            read_dirs=[Path("related/acme/lib")],
        )


@dataclass
class _World:
    app: LocalRepo
    lib: LocalRepo
    github: FakeGitHub
    reviewer: FakeReviewer
    clock: Clock
    state: StateStore
    cache: FakeRepoCache

    def pipeline(self, config: Config) -> Pipeline:
        notifier = FakeNotifier()
        return Pipeline(
            config=config,
            github=self.github,
            repos=self.cache,
            reviewer=self.reviewer,
            notifier=notifier,
            state=self.state,
            auth=AuthChecker(state=self.state, notifier=notifier, launcher=None),
            context=FakeContextFetcher(),
            approval_launcher=None,
            clock=self.clock,
            dry_run=False,
        )


def _world(tmp_path: Path, body: str) -> _World:
    app = make_local_repo(tmp_path / "app")
    base = commit_files(app, {"src/app.py": "a = 1\n"})
    head = commit_files(app, {"src/app.py": "a = 2\n"})
    publish(app)
    lib_root = tmp_path / "lib"
    lib_root.mkdir()
    (lib_root / "work").mkdir()
    lib = LocalRepo(
        work=lib_root / "work", bare=app.bare_root / "acme" / "lib.git", bare_root=app.bare_root
    )
    git(lib.work, "init", "-q", "-b", "main")
    commit_files(lib, {"AGENTS.md": "lib guide\n", "schema/tenant.json": '{"people": "url"}\n'})
    publish(lib)
    github = FakeGitHub([make_pr(1, head, base, body=body)], diff_between(app, base, head))
    github.repositories["acme/lib"] = RepositoryInfo(full_name="acme/lib", default_branch="main")
    linked = make_pr(471, "c" * 40, "d" * 40).model_copy(
        update={"repo": "lib", "url": "https://github.com/acme/lib/pull/471", "title": "Serve URL"}
    )
    github.linked["acme/lib#471"] = PullRequestWithState(pr=linked, state="merged")
    github.linked_diffs["acme/lib#471"] = "diff --git a/x b/x\n+people\n"
    clock = Clock(now=datetime(2026, 11, 14, 12, 0, tzinfo=UTC))
    return _World(
        app=app,
        lib=lib,
        github=github,
        reviewer=FakeReviewer(
            draft=empty_draft(related_sources_used=["acme/lib:schema/tenant.json"])
        ),
        clock=clock,
        state=StateStore(tmp_path / "state.db", clock=clock),
        cache=FakeRepoCache(
            git_binary=Path(GIT),
            bare_root=app.bare_root,
            worktree_root=tmp_path / "wt",
            related_root=tmp_path / "related",
        ),
    )


async def test_review_sees_related_checkout_guide_and_linked_pr(tmp_path: Path) -> None:
    world = _world(tmp_path, "Deploy after acme/lib#471. Not other/repo#2.")

    summary = await world.pipeline(make_config(**RELATED)).run()

    assert summary.outcomes[0].action == Action.POSTED
    related = world.reviewer.contexts[0].related
    assert related is not None
    checkout = related.repos[0].checkout
    assert checkout is not None
    assert (checkout.path / "schema" / "tenant.json").read_text() == '{"people": "url"}\n'
    assert checkout.guide is not None
    assert checkout.guide.text == "lib guide\n"
    assert [(pr.number, pr.state) for pr in related.linked_prs] == [(471, "merged")]
    assert related.readable_paths == [checkout.path]
    body = world.github.created[0].body
    assert "Related context: `acme/lib@" in body
    assert "acme/lib#471 (merged)" in body
    assert "Related sources used: `acme/lib:schema/tenant.json`" in body


async def test_checkout_is_reused_until_max_age_then_refreshed(tmp_path: Path) -> None:
    world = _world(tmp_path, "")
    config = make_config(**RELATED)
    await world.pipeline(config).run()
    first = world.reviewer.contexts[0].related
    assert first is not None and first.repos[0].checkout is not None

    new_sha = commit_files(world.lib, {"AGENTS.md": "lib guide v2\n"})
    publish(world.lib)
    del world.github.repositories["acme/lib"]
    world.clock.now += timedelta(hours=1)
    await world.pipeline(config).review_one(world.github.prs[0], force=True)
    reused = world.reviewer.contexts[1].related
    assert reused is not None and reused.repos[0].checkout is not None
    assert reused.repos[0].checkout.commit_sha == first.repos[0].checkout.commit_sha

    world.github.repositories["acme/lib"] = RepositoryInfo(
        full_name="acme/lib", default_branch="main"
    )
    world.clock.now += timedelta(hours=12)
    await world.pipeline(config).review_one(world.github.prs[0], force=True)
    refreshed = world.reviewer.contexts[2].related
    assert refreshed is not None and refreshed.repos[0].checkout is not None
    assert refreshed.repos[0].checkout.commit_sha == new_sha
    assert refreshed.repos[0].checkout.guide is not None
    assert refreshed.repos[0].checkout.guide.text == "lib guide v2\n"


async def test_unavailable_related_repo_does_not_block_the_review(tmp_path: Path) -> None:
    world = _world(tmp_path, "")
    del world.github.repositories["acme/lib"]

    summary = await world.pipeline(make_config(**RELATED)).run()

    assert summary.outcomes[0].action == Action.POSTED
    assert "`acme/lib` (**unavailable**" in world.github.created[0].body


async def test_prune_removes_checkouts_no_longer_related(tmp_path: Path) -> None:
    world = _world(tmp_path, "")
    await world.pipeline(make_config(**RELATED)).run()
    path = world.cache.related_path("acme/lib")
    assert path.is_dir()

    await world.cache.prune_related({"acme/lib"})
    assert path.is_dir()
    await world.cache.prune_related(set())
    assert not path.exists()
    assert not world.cache.related_stamp_path("acme/lib").exists()
