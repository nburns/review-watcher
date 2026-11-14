import re
from importlib import resources
from typing import Literal

LensName = Literal["sre"]
_HEADER_RE = re.compile(r"\A<!--.*?-->\s*", re.DOTALL)


def load_lens(name: LensName) -> str:
    text = resources.files(__package__).joinpath(f"{name}.md").read_text(encoding="utf-8")
    return _HEADER_RE.sub("", text).strip()
