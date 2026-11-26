"""Configuration, read from the YAML file named by LIADS_CONFIG."""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator

from .versions import DEFAULT_VERSION

# w_organization_social is needed for the hidden (dark) posts behind image ads;
# the server only ever creates posts with feedDistribution NONE.
DEFAULT_SCOPES = ["r_ads", "rw_ads", "r_ads_reporting", "rw_conversions", "w_organization_social"]


class LinkedInSection(BaseModel):
    client_path: Path
    token_path: Path
    api_version: str = DEFAULT_VERSION
    redirect_port: int = 8787
    scopes: list[str] = Field(default_factory=lambda: list(DEFAULT_SCOPES))

    @field_validator("api_version")
    @classmethod
    def _version_format(cls, v: str) -> str:
        if len(v) != 6 or not v.isdigit():
            raise ValueError("api_version must be YYYYMM, e.g. 202610")
        return v


class AdsSection(BaseModel):
    # Ad account IDs (numbers, without the urn prefix). Empty: every write is refused.
    allowed_accounts: list[int] = Field(default_factory=list)


class SafetySection(BaseModel):
    max_daily_budget: Decimal
    max_lifetime_budget: Decimal
    max_bid: Decimal
    log_file: Path
    plans_path: Path | None = None
    plan_ttl_hours: int = 24


class UploadsSection(BaseModel):
    # Images and conversion CSVs are read only from inside this folder.
    allowed_dir: Path


class Config(BaseModel):
    linkedin: LinkedInSection
    ads: AdsSection = Field(default_factory=AdsSection)
    safety: SafetySection
    uploads: UploadsSection

    @property
    def plans_file(self) -> Path:
        return self.safety.plans_path or self.safety.log_file.with_name("plans.json")


def load_config(path: str | os.PathLike | None = None) -> Config:
    path = path or os.environ.get("LIADS_CONFIG")
    if not path:
        raise RuntimeError("Set LIADS_CONFIG to the path of config.yaml")
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return Config.model_validate(data)
