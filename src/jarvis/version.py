from __future__ import annotations

import re
from dataclasses import dataclass
from functools import total_ordering
from pathlib import Path

SEMVER_PATTERN = re.compile(
    r"^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<prerelease>[0-9A-Za-z.-]+))?"
    r"(?:\+(?P<build>[0-9A-Za-z.-]+))?$"
)


@total_ordering
@dataclass(frozen=True)
class SemanticVersion:
    major: int
    minor: int
    patch: int
    prerelease: tuple[str, ...] = ()
    build: tuple[str, ...] = ()

    @classmethod
    def parse(cls, value: str) -> SemanticVersion:
        match = SEMVER_PATTERN.fullmatch(value.strip())
        if match is None:
            raise ValueError(f"Invalid semantic version: {value!r}")
        prerelease = tuple(
            item for item in (match.group("prerelease") or "").split(".") if item
        )
        build = tuple(item for item in (match.group("build") or "").split(".") if item)
        return cls(
            major=int(match.group("major")),
            minor=int(match.group("minor")),
            patch=int(match.group("patch")),
            prerelease=prerelease,
            build=build,
        )

    def __str__(self) -> str:
        base = f"{self.major}.{self.minor}.{self.patch}"
        if self.prerelease:
            base += f"-{'.'.join(self.prerelease)}"
        if self.build:
            base += f"+{'.'.join(self.build)}"
        return base

    def _compare_tuple(self) -> tuple[int, int, int]:
        return self.major, self.minor, self.patch

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, SemanticVersion):
            return NotImplemented
        if self._compare_tuple() != other._compare_tuple():
            return self._compare_tuple() < other._compare_tuple()
        if self.prerelease == other.prerelease:
            return False
        if not self.prerelease:
            return False
        if not other.prerelease:
            return True
        for left, right in zip(self.prerelease, other.prerelease, strict=False):
            if left == right:
                continue
            left_number = left.isdigit()
            right_number = right.isdigit()
            if left_number and right_number:
                return int(left) < int(right)
            if left_number != right_number:
                return left_number
            return left < right
        return len(self.prerelease) < len(other.prerelease)


def read_version_file(path: Path) -> str:
    value = path.expanduser().resolve().read_text(encoding="utf-8").strip()
    SemanticVersion.parse(value)
    return value


VERSION_FILE = Path(__file__).resolve().parents[2] / "VERSION"
__version__ = (
    read_version_file(VERSION_FILE)
    if VERSION_FILE.exists()
    else "1.0.0"
)


def current_version() -> SemanticVersion:
    return SemanticVersion.parse(__version__)


def version_info() -> dict[str, str | int]:
    version = current_version()
    return {
        "version": str(version),
        "major": version.major,
        "minor": version.minor,
        "patch": version.patch,
    }
