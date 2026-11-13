import pytest

from review_watcher.config import GcloudLogsSource
from review_watcher.context import (
    ContextError,
    mentioned_tables,
    redact,
    render_gcloud_entries,
    select_database_url,
)


def test_database_url_is_chosen_by_host_and_credentials_are_unquoted() -> None:
    urls = "mysql://other:x@10.0.0.1/a,mysql://ro%40user:p%2Fw@10.0.0.2:3307/app?charset=utf8"

    url = select_database_url(urls, "10.0.0.2")

    assert (url.user, url.password, url.database, url.port) == ("ro@user", "p/w", "app", 3307)
    with pytest.raises(ContextError, match=r"10\.0\.0\.9"):
        select_database_url(urls, "10.0.0.9")


def test_tables_are_matched_from_model_names_in_the_diff() -> None:
    diff = "+class CheckinComment(Model):\n+    user_id = ForeignKey(User)\n"

    found = mentioned_tables(["checkin_comments", "users", "orders"], diff, limit=10)

    assert found == ["checkin_comments", "users"]


def test_repeated_log_entries_are_grouped_with_secrets_and_people_redacted() -> None:
    source = GcloudLogsSource(kind="gcloud_logs", project="prod", filter="severity>=ERROR")
    entries = [
        {
            "timestamp": "2026-11-06T18:00:00Z",
            "severity": "ERROR",
            "resource": {"type": "cloud_run_revision", "labels": {"service_name": "async"}},
            "jsonPayload": {"message": "save failed for jane@example.com with Bearer abc.def"},
        }
    ]

    entries.append({**entries[0], "timestamp": "2026-11-06T19:00:00Z"})

    text = redact(render_gcloud_entries(entries, source))

    assert (
        "2x ERROR async [2026-11-06T18:00:00Z .. 2026-11-06T19:00:00Z]: "
        "save failed for [email] with Bearer [token]"
    ) in text
    assert "jane@example.com" not in text
