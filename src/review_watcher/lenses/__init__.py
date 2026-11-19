import re
from collections.abc import Sequence
from importlib import resources
from pathlib import PurePosixPath
from typing import Literal

from review_watcher.models import LensPass
from review_watcher.skills import SkillSetName, load_skill_set

LensName = Literal["sre"]
LENS_SKILL_SETS: dict[LensName, tuple[SkillSetName, ...]] = {"sre": ("sre",)}
NON_RUNTIME_PATTERNS: tuple[str, ...] = (
    "**/*.md",
    "**/*.rst",
    "**/docs/**",
    "**/tests/**",
    "**/test/**",
    "**/__tests__/**",
    "**/test_*.py",
    "**/*_test.*",
    "**/*.test.*",
    "**/*.spec.*",
)
_HEADER_RE = re.compile(r"\A<!--.*?-->\s*", re.DOTALL)


def load_lens(name: LensName) -> str:
    text = resources.files(__package__).joinpath(f"{name}.md").read_text(encoding="utf-8")
    return _HEADER_RE.sub("", text).strip()


def load_lens_pass(name: LensName) -> LensPass:
    skill_sets = [load_skill_set(set_name) for set_name in LENS_SKILL_SETS[name]]
    return LensPass(
        name=name,
        text=load_lens(name),
        skill_dirs=[skill_set.path for skill_set in skill_sets],
        skills=[skill for skill_set in skill_sets for skill in skill_set.skills],
    )


def has_runtime_change(changed_files: Sequence[str]) -> bool:
    return any(
        not any(PurePosixPath(path).full_match(pattern) for pattern in NON_RUNTIME_PATTERNS)
        for path in changed_files
    )
