"""Loads local JSONL fixture datasets, tolerating malformed lines.

Each line of a dataset is validated independently. A bad line is skipped and
recorded as an issue instead of failing the whole dataset, so tools can report
partial evidence rather than none.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Generic, TypeVar

from pydantic import BaseModel, ValidationError

DEFAULT_FIXTURES_DIR = Path(__file__).resolve().parents[2] / "data" / "fixtures"

ModelT = TypeVar("ModelT", bound=BaseModel)


class DatasetUnavailableError(Exception):
    """The dataset file is missing or unreadable."""


@dataclass(frozen=True)
class LoadedDataset(Generic[ModelT]):
    name: str
    records: tuple[ModelT, ...]
    # One human-readable entry per skipped line, e.g. "logs.jsonl line 7: level: Input should be ...".
    issues: tuple[str, ...]


def summarize_validation_error(exc: ValidationError, max_errors: int = 3) -> str:
    """Field locations and messages only; input values are left out so data is not echoed into logs."""
    parts = []
    for err in exc.errors(include_url=False)[:max_errors]:
        loc = ".".join(str(part) for part in err["loc"]) or "(root)"
        parts.append(f"{loc}: {err['msg']}")
    if exc.error_count() > max_errors:
        parts.append(f"and {exc.error_count() - max_errors} more")
    return "; ".join(parts)


class FixtureStore:
    """Read-only access to the fixture datasets in one directory. Parsed datasets are cached."""

    def __init__(self, data_dir: Path = DEFAULT_FIXTURES_DIR) -> None:
        self.data_dir = Path(data_dir)
        self._cache: dict[tuple[str, type[BaseModel]], LoadedDataset] = {}

    def load(self, name: str, model: type[ModelT]) -> LoadedDataset[ModelT]:
        key = (name, model)
        if key not in self._cache:
            self._cache[key] = self._read(name, model)
        return self._cache[key]

    def _read(self, name: str, model: type[ModelT]) -> LoadedDataset[ModelT]:
        path = self.data_dir / name
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise DatasetUnavailableError(f"Dataset '{name}' was not found.") from None
        except (OSError, UnicodeDecodeError) as exc:
            raise DatasetUnavailableError(
                f"Dataset '{name}' could not be read ({type(exc).__name__})."
            ) from None

        records: list[ModelT] = []
        issues: list[str] = []
        for lineno, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                records.append(model.model_validate_json(line))
            except ValidationError as exc:
                issues.append(f"{name} line {lineno}: {summarize_validation_error(exc)}")
        return LoadedDataset(name=name, records=tuple(records), issues=tuple(issues))
