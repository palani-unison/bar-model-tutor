"""Critic agent — looks at rendered frames, returns fixes. (Stub for Nov 2 milestone.)

Kept intentionally small: the contract is fixed now so plan/render work can
proceed against it. Returns no fixes until the vision critic is wired up.
"""
from __future__ import annotations


def critique_frames(frame_paths: list[str], spec: dict) -> list[dict]:
    """Return a list of fixes: [{"step": int, "issue": str, "fix": str}]."""
    return []
