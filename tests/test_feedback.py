from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from review_watcher.feedback import FeedbackWatcher
from review_watcher.models import FeedbackItem, FeedbackKind
from review_watcher.state import StateStore
from tests.fakes import FakeGitHub, FakeNotifier, make_config, make_pr

START = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
PR_URL = "https://github.com/acme/app/pull/7"


@dataclass
class Clock:
    now: datetime

    def __call__(self) -> datetime:
        return self.now


@dataclass
class World:
    github: FakeGitHub
    notifier: FakeNotifier
    state: StateStore
    clock: Clock

    def watcher(self, **config: object) -> FeedbackWatcher:
        return FeedbackWatcher(
            config=make_config(**config),
            github=self.github,
            notifier=self.notifier,
            state=self.state,
            clock=self.clock,
        )


def _world(tmp_path: Path) -> World:
    github = FakeGitHub(prs=[], diff_text="")
    github.authored = [make_pr(7, "h" * 40, "b" * 40).model_copy(update={"owner": "Acme"})]
    clock = Clock(now=START)
    state = StateStore(tmp_path / "state.db", clock=clock)
    state.feedback_since()
    clock.now = START + timedelta(minutes=10)
    return World(github=github, notifier=FakeNotifier(), state=state, clock=clock)


def _item(
    item_id: int,
    *,
    kind: FeedbackKind = FeedbackKind.COMMENT,
    author: str = "alice",
    state: str | None = None,
    at: datetime = START + timedelta(minutes=5),
) -> FeedbackItem:
    return FeedbackItem(
        kind=kind,
        item_id=item_id,
        author=author,
        review_state=state,
        url=f"{PR_URL}#{kind.value}-{item_id}",
        created_at=at,
    )


async def test_new_feedback_notifies_once_and_ignores_pre_existing(tmp_path: Path) -> None:
    world = _world(tmp_path)
    world.github.feedback[7] = [_item(1, at=START - timedelta(days=1))]

    await world.watcher().run()
    assert world.notifier.sent == []

    world.github.feedback[7].append(_item(2, at=world.clock.now))
    world.clock.now += timedelta(minutes=5)
    await world.watcher().run()
    await world.watcher().run()

    assert len(world.notifier.sent) == 1
    sent = world.notifier.sent[0]
    assert sent.title == "New feedback: Acme/app#7"
    assert sent.subtitle == "1 comment from alice"
    assert sent.open_url == f"{PR_URL}#comment-2"


async def test_own_and_ignored_authors_do_not_notify(tmp_path: Path) -> None:
    world = _world(tmp_path)
    world.github.feedback[7] = [
        _item(1, author="OctoCat"),
        _item(2, author="codecov[bot]"),
    ]

    await world.watcher(feedback={"ignore_authors": ["codecov[bot]"]}).run()

    assert world.notifier.sent == []


async def test_notification_opens_changes_requested_review(tmp_path: Path) -> None:
    world = _world(tmp_path)
    world.github.feedback[7] = [
        _item(1, kind=FeedbackKind.INLINE_COMMENT, author="bob", at=START + timedelta(minutes=1)),
        _item(2, kind=FeedbackKind.REVIEW, author="carol", state="COMMENTED"),
        _item(3, kind=FeedbackKind.REVIEW, author="bob", state="CHANGES_REQUESTED"),
    ]

    await world.watcher().run()

    sent = world.notifier.sent[0]
    assert sent.title == "Changes requested: Acme/app#7"
    assert sent.subtitle == "2 reviews, 1 inline comment from bob, carol"
    assert sent.open_url == f"{PR_URL}#review-3"


async def test_burst_is_batched_until_quiet(tmp_path: Path) -> None:
    world = _world(tmp_path)
    world.github.feedback[7] = [_item(1, at=world.clock.now - timedelta(minutes=1))]

    await world.watcher().run()
    assert world.notifier.sent == []

    world.github.feedback[7].append(_item(2, at=world.clock.now))
    world.clock.now += timedelta(minutes=4)
    await world.watcher().run()
    assert world.notifier.sent == []

    world.clock.now += timedelta(minutes=1)
    await world.watcher().run()

    assert len(world.notifier.sent) == 1
    assert world.notifier.sent[0].subtitle == "2 comments from alice"


async def test_continuous_feedback_notifies_after_max_wait(tmp_path: Path) -> None:
    world = _world(tmp_path)
    first_at = world.clock.now
    world.clock.now += timedelta(minutes=30)
    world.github.feedback[7] = [_item(1, at=first_at), _item(2, at=world.clock.now)]

    await world.watcher().run()

    assert len(world.notifier.sent) == 1


async def test_disabled_does_nothing(tmp_path: Path) -> None:
    world = _world(tmp_path)
    world.github.feedback[7] = [_item(1)]

    summary = await world.watcher(feedback={"enabled": False}).run()

    assert summary.outcomes == ()
    assert world.notifier.sent == []
