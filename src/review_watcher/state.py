import sqlite3
from collections.abc import Callable, Generator, Iterable
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path

ERROR_NOTIFY_WINDOW = timedelta(hours=6)
_FEEDBACK_SINCE_KEY = "feedback_since"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reviews (
    repo TEXT NOT NULL,
    pr_number INTEGER NOT NULL,
    head_sha TEXT NOT NULL,
    status TEXT NOT NULL,
    review_id INTEGER,
    review_url TEXT,
    risk TEXT,
    guidance_changed INTEGER NOT NULL,
    guidance_tier_max TEXT,
    error TEXT,
    attempts INTEGER NOT NULL,
    timeouts INTEGER NOT NULL DEFAULT 0,
    first_seen_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (repo, pr_number, head_sha)
);
CREATE TABLE IF NOT EXISTS feedback_seen (
    item_key TEXT PRIMARY KEY,
    repo TEXT NOT NULL,
    pr_number INTEGER NOT NULL,
    seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS error_notifications (
    error_key TEXT PRIMARY KEY,
    message TEXT NOT NULL,
    sent_at TEXT NOT NULL
);
"""


class ReviewStatus(StrEnum):
    PENDING_DEBOUNCE = "pending_debounce"
    POSTED = "posted"
    SKIPPED_EXISTING_PENDING = "skipped_existing_pending"
    FAILED = "failed"


@dataclass(frozen=True)
class ReviewRecord:
    repo: str
    pr_number: int
    head_sha: str
    status: ReviewStatus
    review_id: int | None
    review_url: str | None
    risk: str | None
    guidance_changed: bool
    guidance_tier_max: str | None
    error: str | None
    attempts: int
    timeouts: int
    first_seen_at: datetime
    created_at: datetime
    updated_at: datetime


class StateStore:
    def __init__(self, path: Path, clock: Callable[[], datetime]) -> None:
        self._path = path
        self._clock = clock
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(_SCHEMA)
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(reviews)")}
            if "timeouts" not in columns:
                connection.execute(
                    "ALTER TABLE reviews ADD COLUMN timeouts INTEGER NOT NULL DEFAULT 0"
                )

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection]:
        connection = sqlite3.connect(self._path)
        connection.row_factory = sqlite3.Row
        with closing(connection), connection:
            yield connection

    def get(self, repo: str, pr_number: int, head_sha: str) -> ReviewRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM reviews WHERE repo = ? AND pr_number = ? AND head_sha = ?",
                (repo, pr_number, head_sha),
            ).fetchone()
        if row is None:
            return None
        return _record_from_row(row)

    def history(self, repo: str, pr_number: int) -> list[ReviewRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM reviews WHERE repo = ? AND pr_number = ?",
                (repo, pr_number),
            ).fetchall()
        return [_record_from_row(row) for row in rows]

    def save(self, record: ReviewRecord) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO reviews (
                    repo, pr_number, head_sha, status, review_id, review_url, risk,
                    guidance_changed, guidance_tier_max, error, attempts, timeouts,
                    first_seen_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (repo, pr_number, head_sha) DO UPDATE SET
                    status = excluded.status,
                    review_id = excluded.review_id,
                    review_url = excluded.review_url,
                    risk = excluded.risk,
                    guidance_changed = excluded.guidance_changed,
                    guidance_tier_max = excluded.guidance_tier_max,
                    error = excluded.error,
                    attempts = excluded.attempts,
                    timeouts = excluded.timeouts,
                    updated_at = excluded.updated_at
                """,
                (
                    record.repo,
                    record.pr_number,
                    record.head_sha,
                    record.status.value,
                    record.review_id,
                    record.review_url,
                    record.risk,
                    int(record.guidance_changed),
                    record.guidance_tier_max,
                    record.error,
                    record.attempts,
                    record.timeouts,
                    record.first_seen_at.isoformat(),
                    record.created_at.isoformat(),
                    record.updated_at.isoformat(),
                ),
            )

    def should_notify_error(self, error_key: str, message: str) -> bool:
        now = self._clock()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT message, sent_at FROM error_notifications WHERE error_key = ?",
                (error_key,),
            ).fetchone()
            if row is not None:
                same = row["message"] == message
                recent = now - datetime.fromisoformat(row["sent_at"]) < ERROR_NOTIFY_WINDOW
                if same and recent:
                    return False
            connection.execute(
                """
                INSERT INTO error_notifications (error_key, message, sent_at) VALUES (?, ?, ?)
                ON CONFLICT (error_key) DO UPDATE SET message = excluded.message,
                    sent_at = excluded.sent_at
                """,
                (error_key, message, now.isoformat()),
            )
        return True

    def feedback_since(self) -> datetime:
        """When feedback watching started; earlier feedback is treated as already seen."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM meta WHERE key = ?", (_FEEDBACK_SINCE_KEY,)
            ).fetchone()
            if row is not None:
                return datetime.fromisoformat(row["value"])
            now = self._clock()
            connection.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?)",
                (_FEEDBACK_SINCE_KEY, now.isoformat()),
            )
            return now

    def unseen_feedback_keys(self, keys: Iterable[str]) -> set[str]:
        wanted = set(keys)
        if len(wanted) == 0:
            return wanted
        placeholders = ",".join("?" for _ in wanted)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT item_key FROM feedback_seen WHERE item_key IN ({placeholders})",  # noqa: S608
                tuple(wanted),
            ).fetchall()
        return wanted - {row["item_key"] for row in rows}

    def mark_feedback_seen(self, repo: str, pr_number: int, keys: Iterable[str]) -> None:
        now = self._clock().isoformat()
        with self._connect() as connection:
            connection.executemany(
                """
                INSERT INTO feedback_seen (item_key, repo, pr_number, seen_at) VALUES (?, ?, ?, ?)
                ON CONFLICT (item_key) DO NOTHING
                """,
                [(key, repo, pr_number, now) for key in keys],
            )

    def prune(self, older_than: timedelta) -> int:
        cutoff = (self._clock() - older_than).isoformat()
        with self._connect() as connection:
            cursor = connection.execute("DELETE FROM reviews WHERE updated_at < ?", (cutoff,))
            return cursor.rowcount


def utc_now() -> datetime:
    return datetime.now(UTC)


def _record_from_row(row: sqlite3.Row) -> ReviewRecord:
    return ReviewRecord(
        repo=row["repo"],
        pr_number=row["pr_number"],
        head_sha=row["head_sha"],
        status=ReviewStatus(row["status"]),
        review_id=row["review_id"],
        review_url=row["review_url"],
        risk=row["risk"],
        guidance_changed=bool(row["guidance_changed"]),
        guidance_tier_max=row["guidance_tier_max"],
        error=row["error"],
        attempts=row["attempts"],
        timeouts=row["timeouts"],
        first_seen_at=datetime.fromisoformat(row["first_seen_at"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )
