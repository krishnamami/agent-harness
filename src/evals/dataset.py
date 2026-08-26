"""Golden sets.

A golden set is identified by the **hash of its contents**, not by a version
string someone remembers to bump. Every run records that hash.

This is the subtlety that trips up most eval setups. Scores are only comparable
across runs of the *same* dataset. Add ten cases, and today's 0.91 is not
better than last week's 0.88 -- it is a different measurement wearing the same
name. Recording the hash makes that visible instead of letting a quiet dataset
edit look like a quality improvement.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evals.protocol import EvalCase


@dataclass(frozen=True)
class DatasetInfo:
    """Identity of a golden set at a point in time."""

    name: str
    sha256: str
    case_count: int

    @property
    def short_sha(self) -> str:
        return self.sha256[:12]

    def __str__(self) -> str:
        return f"{self.name}@{self.short_sha} ({self.case_count} cases)"


@dataclass(frozen=True)
class Dataset:
    info: DatasetInfo
    cases: tuple[EvalCase, ...]

    def filter_by_tag(self, tag: str) -> Dataset:
        """Narrow to a slice.

        Slices matter more than the overall number: an aggregate that is fine
        while one tag is failing is the normal way a regression hides.
        """
        kept = tuple(c for c in self.cases if tag in c.tags)
        return Dataset(
            info=DatasetInfo(
                name=f"{self.info.name}[{tag}]",
                sha256=self.info.sha256,
                case_count=len(kept),
            ),
            cases=kept,
        )

    @property
    def tags(self) -> tuple[str, ...]:
        seen: set[str] = set()
        for case in self.cases:
            seen.update(case.tags)
        return tuple(sorted(seen))


def load_jsonl(path: Path | str, name: str | None = None) -> Dataset:
    """Load a golden set from JSON Lines.

    JSONL rather than JSON: one case per line means a diff shows which cases
    changed, and appending a case does not reindent the whole file.
    """
    path = Path(path)
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()

    cases: list[EvalCase] = []
    seen_ids: set[str] = set()

    for lineno, line in enumerate(raw.decode("utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("//"):
            continue
        try:
            record: dict[str, Any] = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{lineno} is not valid JSON: {exc}") from exc

        case_id = record.get("id")
        if not case_id:
            raise ValueError(f"{path}:{lineno} has no 'id'")
        if case_id in seen_ids:
            # Duplicate ids silently double-weight a case in every aggregate.
            raise ValueError(f"{path}:{lineno} repeats id {case_id!r}")
        seen_ids.add(case_id)

        cases.append(
            EvalCase(
                id=case_id,
                inputs=record.get("inputs", {}),
                expected=record.get("expected"),
                tags=tuple(record.get("tags", ())),
                metadata=record.get("metadata", {}),
            )
        )

    if not cases:
        raise ValueError(f"{path} contains no cases")

    return Dataset(
        info=DatasetInfo(name=name or path.stem, sha256=digest, case_count=len(cases)),
        cases=tuple(cases),
    )
