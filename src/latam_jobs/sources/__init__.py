"""Registered job sources. Add a board by importing its adapter and listing it here."""

from __future__ import annotations

from latam_jobs.sources.arbeitnow import ArbeitnowSource
from latam_jobs.sources.ashby import AshbySource
from latam_jobs.sources.base import JobSource
from latam_jobs.sources.getonboard import GetOnBoardSource
from latam_jobs.sources.greenhouse import GreenhouseSource
from latam_jobs.sources.himalayas import HimalayasSource
from latam_jobs.sources.jobicy import JobicySource
from latam_jobs.sources.lever import LeverSource
from latam_jobs.sources.remoteok import RemoteOkSource
from latam_jobs.sources.remotive import RemotiveSource
from latam_jobs.sources.wwr import WwrSource

SOURCES: dict[str, JobSource] = {
    RemotiveSource.name: RemotiveSource(),
    HimalayasSource.name: HimalayasSource(),
    GetOnBoardSource.name: GetOnBoardSource(),
    WwrSource.name: WwrSource(),
    RemoteOkSource.name: RemoteOkSource(),
    JobicySource.name: JobicySource(),
    ArbeitnowSource.name: ArbeitnowSource(),
    GreenhouseSource.name: GreenhouseSource(),
    LeverSource.name: LeverSource(),
    AshbySource.name: AshbySource(),
}
