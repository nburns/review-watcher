import asyncio
import json
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, cast
from urllib.parse import unquote, urlsplit

from review_watcher.config import (
    AuthConfig,
    AwsLogsSource,
    ContextSource,
    GcloudLogsSource,
    HoopSchemaSource,
    MysqlSchemaSource,
)
from review_watcher.models import ContextResult, PullRequestRef
from review_watcher.proc import base_env, run_command

CONTEXT_TIMEOUT_S = 120.0
_MESSAGE_LIMIT = 500
_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_TABLE_NAME_RE = re.compile(r"^[A-Za-z0-9_$]+$")
_DIGITS_RE = re.compile(r"\d+")
_CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_MYSQL_READ_ONLY = "SET SESSION TRANSACTION READ ONLY; "
_MYSQL_TABLES_SQL = (
    "SELECT TABLE_NAME, TABLE_ROWS FROM information_schema.TABLES "
    "WHERE TABLE_SCHEMA = DATABASE() ORDER BY TABLE_NAME;"
)
_PG_TABLES_SQL = "SELECT relname, n_live_tup FROM pg_stat_user_tables ORDER BY relname;"
_PG_COLUMNS_SQL = (
    "SELECT table_name, column_name, data_type, is_nullable, column_default "
    "FROM information_schema.columns WHERE table_schema = 'public' "
    "AND table_name IN ({names}) ORDER BY table_name, ordinal_position;"
)
_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[email]"),
    (re.compile(r"(?i)bearer\s+[\w.~+/=-]+"), "Bearer [token]"),
    (re.compile(r"eyJ[\w-]+\.[\w-]+\.[\w-]+"), "[jwt]"),
    (re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"), "[aws-key]"),
    (re.compile(r"\b[0-9a-fA-F]{32,}\b"), "[hex]"),
    (re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}"), "[base64]"),
)

SOURCE_TOOLS: Mapping[str, tuple[str, ...]] = {
    "gcloud_logs": ("gcloud",),
    "aws_logs": ("aws",),
    "mysql_schema": ("gcloud", "mysql"),
    "hoop_schema": ("hoop",),
}

logger = logging.getLogger(__name__)


class ContextError(RuntimeError):
    pass


class ContextFetcher(Protocol):
    async def fetch(
        self, pr: PullRequestRef, sources: Mapping[str, ContextSource], diff_text: str
    ) -> list[ContextResult]: ...


class CliContextFetcher:
    def __init__(self, auth: Sequence[AuthConfig]) -> None:
        self._auth = {item.name: item for item in auth}

    async def fetch(
        self, pr: PullRequestRef, sources: Mapping[str, ContextSource], diff_text: str
    ) -> list[ContextResult]:
        return list(
            await asyncio.gather(
                *(self._fetch_one(pr, name, source, diff_text) for name, source in sources.items())
            )
        )

    async def _fetch_one(
        self, pr: PullRequestRef, name: str, source: ContextSource, diff_text: str
    ) -> ContextResult:
        env = base_env()
        for auth_name in source.auth:
            env.update(self._auth[auth_name].env)
        try:
            text = await _render_source(source, env, diff_text)
        except Exception as exc:
            # A missing source must not block the review; the review body names it as unavailable.
            logger.exception("context_failed pr=%s source=%s", pr.slug, name)
            return ContextResult(
                name=name, kind=source.kind, text="", truncated=False, error=_first_line(exc)
            )
        clipped, truncated = clip_text(redact(text), source.max_bytes)
        logger.info(
            "context_fetched pr=%s source=%s bytes=%d truncated=%s",
            pr.slug,
            name,
            len(clipped),
            truncated,
        )
        return ContextResult(
            name=name, kind=source.kind, text=clipped, truncated=truncated, error=None
        )


async def _render_source(source: ContextSource, env: dict[str, str], diff_text: str) -> str:
    match source:
        case GcloudLogsSource():
            return await _gcloud_logs(source, env)
        case AwsLogsSource():
            return await _aws_logs(source, env)
        case MysqlSchemaSource():
            return await _mysql_schema(source, env, diff_text)
        case HoopSchemaSource():
            return await _hoop_schema(source, env, diff_text)


async def _run(argv: list[str], env: Mapping[str, str]) -> str:
    result = await run_command(argv, env=env, timeout_s=CONTEXT_TIMEOUT_S)
    return result.stdout.decode("utf-8", errors="replace")


async def _gcloud_logs(source: GcloudLogsSource, env: Mapping[str, str]) -> str:
    raw = await _run(
        [
            "gcloud",
            "logging",
            "read",
            source.filter,
            "--project",
            source.project,
            "--freshness",
            source.freshness,
            "--limit",
            str(source.limit),
            "--format=json",
        ],
        env,
    )
    return render_gcloud_entries(json.loads(raw), source)


def render_gcloud_entries(entries: list[dict[str, Any]], source: GcloudLogsSource) -> str:
    header = (
        f"GCP project {source.project}, filter {source.filter!r}, last {source.freshness}, "
        f"{len(entries)} entries (limit {source.limit})"
    )
    lines: list[_LogLine] = []
    for entry in entries:
        resource = cast("dict[str, Any]", entry.get("resource", {}))
        labels = cast("dict[str, str]", resource.get("labels", {}))
        origin = labels.get("service_name") or labels.get("job_name") or resource.get("type", "?")
        lines.append(
            _LogLine(
                timestamp=str(entry.get("timestamp", "?")),
                prefix=f"{entry.get('severity', '?')} {origin}",
                message=_entry_message(entry),
            )
        )
    return "\n".join([header, *group_log_lines(lines)])


@dataclass(frozen=True)
class _LogLine:
    timestamp: str
    prefix: str
    message: str


def group_log_lines(lines: Sequence[_LogLine]) -> list[str]:
    """Collapse repeats that differ only in numbers, most frequent first."""
    groups: dict[tuple[str, str], list[_LogLine]] = {}
    for line in lines:
        groups.setdefault((line.prefix, _DIGITS_RE.sub("#", line.message)), []).append(line)
    ranked = sorted(groups.values(), key=len, reverse=True)
    rendered: list[str] = []
    for group in ranked:
        stamps = sorted(line.timestamp for line in group)
        span = stamps[0] if len(stamps) == 1 else f"{stamps[0]} .. {stamps[-1]}"
        rendered.append(f"{len(group)}x {group[0].prefix} [{span}]: {group[0].message}")
    return rendered


def _entry_message(entry: Mapping[str, Any]) -> str:
    if "textPayload" in entry:
        message = str(entry["textPayload"])
    elif "jsonPayload" in entry:
        payload = cast("dict[str, Any]", entry["jsonPayload"])
        message = str(payload["message"]) if "message" in payload else json.dumps(payload)
    elif "protoPayload" in entry:
        message = json.dumps(entry["protoPayload"].get("status", entry["protoPayload"]))
    else:
        message = "(no payload)"
    return " ".join(message.split())[:_MESSAGE_LIMIT]


async def _aws_logs(source: AwsLogsSource, env: Mapping[str, str]) -> str:
    start_ms = int((datetime.now(UTC) - timedelta(hours=source.window_hours)).timestamp() * 1000)
    sections: list[str] = []
    for group in source.log_groups:
        argv = [
            "aws",
            "logs",
            "filter-log-events",
            "--profile",
            source.profile,
            "--region",
            source.region,
            "--log-group-name",
            group,
            "--start-time",
            str(start_ms),
            "--max-items",
            str(source.limit),
            "--output",
            "json",
        ]
        if source.filter_pattern != "":
            argv.extend(["--filter-pattern", source.filter_pattern])
        data = json.loads(await _run(argv, env))
        events = cast("list[dict[str, Any]]", data.get("events", []))
        lines = [
            _LogLine(
                timestamp=datetime.fromtimestamp(event["timestamp"] / 1000, UTC).isoformat(),
                prefix=group,
                message=" ".join(str(event.get("message", "")).split())[:_MESSAGE_LIMIT],
            )
            for event in events
        ]
        header = f"{group}: {len(events)} events in the last {source.window_hours}h"
        sections.append("\n".join([header, *group_log_lines(lines)]))
    return f"AWS profile {source.profile} ({source.region})\n\n" + "\n\n".join(sections)


async def _mysql_schema(source: MysqlSchemaSource, env: Mapping[str, str], diff_text: str) -> str:
    secret = await _run(
        [
            "gcloud",
            "secrets",
            "versions",
            "access",
            "latest",
            "--secret",
            source.secret.name,
            "--project",
            source.secret.project,
        ],
        env,
    )
    url = select_database_url(secret, source.host)
    mysql_env = {**env, "MYSQL_PWD": url.password}
    base_argv = [
        "mysql",
        "--host",
        source.host,
        "--port",
        str(url.port if url.port is not None else source.port),
        "--user",
        url.user,
        "--database",
        url.database,
        "--connect-timeout=10",
        "--batch",
        "--raw",
        "--skip-column-names",
        "-e",
    ]
    listing = await _run([*base_argv, _MYSQL_READ_ONLY + _MYSQL_TABLES_SQL], mysql_env)
    rows = _parse_tab_rows(listing)
    tables = mentioned_tables([name for name, _ in rows], diff_text, source.max_tables)
    parts = [f"MySQL database {url.database} on {source.host}"]
    if tables:
        statements = " ".join(f"SHOW CREATE TABLE `{name}`;" for name in tables)
        parts.append(
            "Tables mentioned in the diff:\n"
            + await _run([*base_argv, _MYSQL_READ_ONLY + statements], mysql_env)
        )
    else:
        parts.append("No tables in this database are mentioned in the diff.")
    parts.append(
        "All tables (approximate row counts):\n"
        + "\n".join(f"{name} {count}" for name, count in rows)
    )
    return "\n\n".join(parts)


async def _hoop_schema(source: HoopSchemaSource, env: Mapping[str, str], diff_text: str) -> str:
    sections: list[str] = []
    for connection in source.connections:
        listing = await _run(
            [
                "hoop",
                "exec",
                connection,
                "-i",
                _PG_TABLES_SQL,
            ],
            env,
        )
        rows = [row for row in _parse_tab_rows(listing) if row != ("relname", "n_live_tup")]
        tables = mentioned_tables([name for name, _ in rows], diff_text, source.max_tables)
        lines = [f"Postgres via hoop connection {connection}"]
        if tables:
            names = ", ".join(f"'{name}'" for name in tables)
            columns = await _run(
                [
                    "hoop",
                    "exec",
                    connection,
                    "-i",
                    # Names come from the database listing and passed _TABLE_NAME_RE.
                    _PG_COLUMNS_SQL.format(names=names),
                ],
                env,
            )
            lines.append("Columns of tables mentioned in the diff:\n" + columns.strip())
        else:
            lines.append("No tables in this database are mentioned in the diff.")
        lines.append(
            "All tables (approximate row counts):\n"
            + "\n".join(f"{name} {count}" for name, count in rows)
        )
        sections.append("\n\n".join(lines))
    return "\n\n".join(sections)


def _parse_tab_rows(output: str) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for line in output.splitlines():
        name, separator, value = line.partition("\t")
        if separator and _TABLE_NAME_RE.fullmatch(name):
            rows.append((name, value.strip()))
    return rows


def mentioned_tables(tables: Sequence[str], diff_text: str, limit: int) -> list[str]:
    tokens: set[str] = set()
    for token in _IDENTIFIER_RE.findall(diff_text):
        snake = _CAMEL_BOUNDARY_RE.sub("_", token).lower()
        tokens.update({token.lower(), snake, f"{snake}s", f"{snake}es"})
    found = [table for table in tables if table.lower() in tokens]
    return sorted(found)[:limit]


@dataclass(frozen=True)
class DatabaseUrl:
    user: str
    password: str
    database: str
    port: int | None


def select_database_url(secret: str, host: str) -> DatabaseUrl:
    """Pick the URL for `host` from a secret holding one or more URLs; never echo the secret."""
    for candidate in re.split(r"[\s,]+", secret.strip()):
        if candidate == "":
            continue
        parts = urlsplit(candidate)
        if parts.hostname != host:
            continue
        if parts.username is None or parts.password is None:
            raise ContextError(f"database URL for {host} has no user or password")
        database = parts.path.lstrip("/")
        if database == "":
            raise ContextError(f"database URL for {host} names no database")
        return DatabaseUrl(
            user=unquote(parts.username),
            password=unquote(parts.password),
            database=database,
            port=parts.port,
        )
    raise ContextError(f"no database URL in the secret points at {host}")


def redact(text: str) -> str:
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def clip_text(text: str, max_bytes: int) -> tuple[str, bool]:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text, False
    clipped = encoded[:max_bytes].decode("utf-8", errors="ignore")
    cut = clipped.rfind("\n")
    return (clipped[:cut] if cut > 0 else clipped), True


def _first_line(exc: BaseException) -> str:
    text = str(exc).strip()
    return text.splitlines()[0][:200] if text else type(exc).__name__
