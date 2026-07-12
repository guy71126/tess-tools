from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ResolvedTarget:
    raw: str
    kind: str
    tic_id: str | None = None
    ra_deg: float | None = None
    dec_deg: float | None = None
    tmag: float | None = None
    source: str = "input"
    extra: dict[str, Any] = field(default_factory=dict)

    def label(self) -> str:
        if self.tic_id:
            return f"TIC {self.tic_id}"
        if self.ra_deg is not None and self.dec_deg is not None:
            return f"{self.ra_deg:.6f} {self.dec_deg:.6f}"
        return self.raw

    def to_dict(self) -> dict[str, Any]:
        return {
            "raw": self.raw,
            "kind": self.kind,
            "tic_id": self.tic_id,
            "ra_deg": self.ra_deg,
            "dec_deg": self.dec_deg,
            "tmag": self.tmag,
            "source": self.source,
            "extra": self.extra,
        }


@dataclass(frozen=True)
class SectorRecord:
    sector: int
    camera: int | None = None
    ccd: int | None = None
    sector_name: str | None = None
    cutout_available: bool = True
    source: str = "tesscut"

    def to_dict(self) -> dict[str, Any]:
        return {
            "sector": self.sector,
            "camera": self.camera,
            "ccd": self.ccd,
            "sector_name": self.sector_name,
            "cutout_available": self.cutout_available,
            "source": self.source,
        }


@dataclass(frozen=True)
class ProductRecord:
    family: str
    provider: str
    sector: int | None = None
    cadence_sec: int | None = None
    product_id: str | None = None
    description: str | None = None
    access_url: str | None = None
    source: str = "mast"
    caveats: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "family": self.family,
            "provider": self.provider,
            "sector": self.sector,
            "cadence_sec": self.cadence_sec,
            "product_id": self.product_id,
            "description": self.description,
            "access_url": self.access_url,
            "source": self.source,
            "caveats": self.caveats,
            "extra": self.extra,
        }


@dataclass(frozen=True)
class ProviderStatus:
    name: str
    status: str
    message: str | None = None
    records: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "message": self.message,
            "records": self.records,
        }
