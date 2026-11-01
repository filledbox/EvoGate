"""Read-only SQLite execution helpers."""

import re
import sqlite3
from pathlib import Path

from .sandbox import validate_sql


def schema(database: Path) -> str:
    with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as db:
        return "\n".join(
            row[0]
            for row in db.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND sql IS NOT NULL ORDER BY name"
            )
        )


def extract_sql(content: str):
    content = content.strip()
    if content.startswith("```"):
        match = re.fullmatch(r"```(?:sql|sqlite)?\s*\n(.*?)\n```", content, re.S | re.I)
        if not match:
            raise ValueError("one SQL block required")
        content = match[1].strip()
    validate_sql(content)
    return content
