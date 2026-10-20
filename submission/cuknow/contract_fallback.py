"""Copies of the harness contract types, used only when solution.py runs without the harness."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class Page:
    url: str
    title: str
    markdown: str
    fetched_at: str


@dataclass(frozen=True)
class Snapshot:
    snapshot_id: str
    taken_on: str
    pages: list


@dataclass
class Source:
    url: str
    snapshot_id: Optional[str] = None
    quote: Optional[str] = None


@dataclass
class Answer:
    text: str
    sources: list = field(default_factory=list)
    confident: bool = True
    conflict_note: Optional[str] = None


@dataclass
class Conflict:
    topic: str
    values: list
    sources: list
    resolution: Optional[str] = None
    reason: Optional[str] = None
