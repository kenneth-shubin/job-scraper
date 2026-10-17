"""Registered job sources. Add a board by importing its adapter and listing it here."""

from __future__ import annotations

from latam_jobs.sources.base import JobSource

SOURCES: dict[str, JobSource] = {}
