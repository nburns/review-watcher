import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import cast

import yaml

from review_watcher.models import SkillSummary
from review_watcher.repo_cache import TreeEntry

SKILLS_PREFIX = ".claude/skills/"
SKILL_FILE = "SKILL.md"
MAX_FRONTMATTER_BYTES = 8_000
MAX_DESCRIPTION_CHARS = 600

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Frontmatter:
    name: str | None
    description: str | None


def skill_dir(path: str) -> str | None:
    if not path.startswith(SKILLS_PREFIX):
        return None
    parts = path[len(SKILLS_PREFIX) :].split("/")
    if len(parts) < 2:
        return None
    return SKILLS_PREFIX + parts[0]


def is_review_skill_file(path: str) -> bool:
    directory = skill_dir(path)
    if directory is None or not path.endswith("/" + SKILL_FILE):
        return False
    return "review" in directory.removeprefix(SKILLS_PREFIX).lower()


def parse_frontmatter(text: str) -> Frontmatter:
    if not text.startswith("---"):
        return Frontmatter(name=None, description=None)
    end = text.find("\n---", 3)
    if end == -1:
        return Frontmatter(name=None, description=None)
    try:
        data: object = yaml.safe_load(text[3:end])
    except yaml.YAMLError:
        logger.info("skill_frontmatter_unparseable")
        return Frontmatter(name=None, description=None)
    if not isinstance(data, dict):
        return Frontmatter(name=None, description=None)
    fields = cast("dict[object, object]", data)
    name = fields.get("name")
    description = fields.get("description")
    return Frontmatter(
        name=name.strip() if isinstance(name, str) else None,
        description=(
            " ".join(description.split())[:MAX_DESCRIPTION_CHARS]
            if isinstance(description, str)
            else None
        ),
    )


async def summarize_skills(
    entries: Sequence[TreeEntry], read: Callable[[TreeEntry], Awaitable[bytes]]
) -> list[SkillSummary]:
    by_dir: dict[str, list[TreeEntry]] = {}
    for entry in entries:
        directory = skill_dir(entry.path)
        if directory is not None:
            by_dir.setdefault(directory, []).append(entry)
    summaries: list[SkillSummary] = []
    for directory, members in sorted(by_dir.items()):
        skill_file = next((m for m in members if m.path == f"{directory}/{SKILL_FILE}"), None)
        meta = Frontmatter(name=None, description=None)
        if skill_file is not None and skill_file.is_blob:
            raw = await read(skill_file)
            meta = parse_frontmatter(raw[:MAX_FRONTMATTER_BYTES].decode("utf-8", errors="replace"))
        summaries.append(
            SkillSummary(
                path=directory,
                name=meta.name,
                description=meta.description,
                file_count=len(members),
            )
        )
    return summaries
