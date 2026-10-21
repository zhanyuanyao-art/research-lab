from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class Checkpoint(BaseModel):
    model_config = ConfigDict(extra='forbid')
    scope: str = Field(default='', max_length=4000)
    exclusions: list[str] = Field(default_factory=list, max_length=40)
    uncertainties: list[str] = Field(default_factory=list, max_length=40)
    next_actions: list[str] = Field(default_factory=list, max_length=20)


class Citation(BaseModel):
    model_config = ConfigDict(extra='forbid')
    read_id: str
    supports: str = Field(min_length=1, max_length=1000)


class Draft(BaseModel):
    model_config = ConfigDict(extra='forbid')
    title: str = Field(min_length=1, max_length=180)
    body: str = Field(min_length=20, max_length=50000)
    citations: list[Citation] = Field(default_factory=list, max_length=80)
    checkpoint: Checkpoint
    status: Literal['working', 'review'] = 'review'
    expected_revision: int = Field(ge=0)
