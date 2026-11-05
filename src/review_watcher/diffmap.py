from dataclasses import dataclass, field

from unidiff.patch import PatchSet

from review_watcher.models import Side


@dataclass(frozen=True)
class CommentPosition:
    side: Side
    line: int


@dataclass(frozen=True)
class HunkSpan:
    path: str
    positions: tuple[CommentPosition, ...]


@dataclass
class FileDiff:
    path: str
    text: str
    hunks: list[HunkSpan] = field(default_factory=list)
    is_binary: bool = False


@dataclass(frozen=True)
class DiffMap:
    files: dict[str, FileDiff]

    @property
    def changed_files(self) -> tuple[str, ...]:
        return tuple(self.files)

    def has_position(self, path: str, side: Side, line: int) -> bool:
        return self.hunk_for(path, side, line) is not None

    def hunk_for(self, path: str, side: Side, line: int) -> HunkSpan | None:
        file_diff = self.files.get(path)
        if file_diff is None:
            return None
        target = CommentPosition(side=side, line=line)
        for hunk in file_diff.hunks:
            if target in hunk.positions:
                return hunk
        return None


def parse_diff(diff_text: str) -> DiffMap:
    patch = PatchSet(diff_text)
    files: dict[str, FileDiff] = {}
    for patched in patch:
        path = _patched_path(patched.path, patched.source_file, patched.target_file)
        file_diff = FileDiff(path=path, text=str(patched), is_binary=patched.is_binary_file)
        for hunk in patched:
            positions: list[CommentPosition] = []
            for line in hunk:
                if line.is_added and line.target_line_no is not None:
                    positions.append(CommentPosition(Side.RIGHT, line.target_line_no))
                elif line.is_removed and line.source_line_no is not None:
                    positions.append(CommentPosition(Side.LEFT, line.source_line_no))
                elif line.is_context:
                    if line.target_line_no is not None:
                        positions.append(CommentPosition(Side.RIGHT, line.target_line_no))
                    if line.source_line_no is not None:
                        positions.append(CommentPosition(Side.LEFT, line.source_line_no))
            file_diff.hunks.append(HunkSpan(path=path, positions=tuple(positions)))
        files[path] = file_diff
    return DiffMap(files=files)


def _patched_path(path: str, source_file: str, target_file: str) -> str:
    if target_file == "/dev/null":
        return _strip_prefix(source_file)
    return _strip_prefix(target_file)


def _strip_prefix(name: str) -> str:
    if name.startswith(("a/", "b/")):
        return name[2:]
    return name
