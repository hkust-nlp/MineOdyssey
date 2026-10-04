#!/usr/bin/env python3
"""Atomically materialize messages.json from the append-only messages.jsonl."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterator


class MessageJournalError(ValueError):
    """The committed portion of a message journal is malformed."""


@dataclass(frozen=True)
class FinalizeResult:
    message_count: int
    dropped_incomplete_tail: bool
    journal_path: str
    output_path: str


def _iter_committed_messages(journal_path: Path) -> Iterator[dict]:
    expected_sequence = 0
    with journal_path.open("rb") as handle:
        line_number = 0
        while True:
            raw_line = handle.readline()
            if not raw_line:
                return
            line_number += 1
            if not raw_line.endswith(b"\n"):
                return
            try:
                envelope = json.loads(raw_line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise MessageJournalError(
                    f"invalid committed journal line {line_number}: {error}"
                ) from error
            if not isinstance(envelope, dict):
                raise MessageJournalError(
                    f"journal line {line_number} must contain an object"
                )
            if envelope.get("schema_version") != 1:
                raise MessageJournalError(
                    f"journal line {line_number} has an unsupported schema_version"
                )
            sequence = envelope.get("seq")
            if isinstance(sequence, bool) or sequence != expected_sequence:
                raise MessageJournalError(
                    f"journal line {line_number} expected seq {expected_sequence}, got {sequence!r}"
                )
            message = envelope.get("message")
            if not isinstance(message, dict):
                raise MessageJournalError(
                    f"journal line {line_number} must contain a message object"
                )
            yield message
            expected_sequence += 1


def finalize_agent_messages(record_dir: Path) -> FinalizeResult:
    """Stream a journal into a legacy-compatible JSON array via atomic replace."""
    record_dir = Path(record_dir)
    journal_path = record_dir / "messages.jsonl"
    output_path = record_dir / "messages.json"
    if not journal_path.is_file():
        raise FileNotFoundError(f"message journal not found: {journal_path}")

    dropped_incomplete_tail = False
    with journal_path.open("rb") as source:
        source.seek(0, os.SEEK_END)
        size = source.tell()
        if size:
            source.seek(-1, os.SEEK_END)
            dropped_incomplete_tail = source.read(1) != b"\n"

    record_dir.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".messages.", suffix=".json.tmp", dir=record_dir
    )
    temporary_path = Path(temporary_name)
    message_count = 0
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            output.write("[\n")
            first = True
            for message in _iter_committed_messages(journal_path):
                if not first:
                    output.write(",\n")
                json.dump(
                    message,
                    output,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                first = False
                message_count += 1
            output.write("\n]\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_path, output_path)
        directory_descriptor = os.open(record_dir, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise

    return FinalizeResult(
        message_count=message_count,
        dropped_incomplete_tail=dropped_incomplete_tail,
        journal_path=str(journal_path),
        output_path=str(output_path),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--record-dir", type=Path, required=True)
    args = parser.parse_args()
    result = finalize_agent_messages(args.record_dir)
    print(json.dumps(asdict(result), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
