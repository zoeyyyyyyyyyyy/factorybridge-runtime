"""Frozen HW7 §5.1 wire types; no benchmark cases or answer keys live here."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Literal

PartKind = Literal['print', 'pcb', 'purchased']
RFQKind = Literal['print', 'pcb', 'purchased', 'shipping']
Status = Literal['on_track', 'delayed', 'delivered', 'failed', 'cancelled']

@dataclass
class Part:
    part_id: str
    kind: PartKind
    qty: int
    material: str | None = None
    volume_cm3: float | None = None
    max_dim_mm: float | None = None
    tolerance_mm: float | None = None
    layers: int | None = None
    size_mm: tuple[float, ...] | None = None
    sku: str | None = None

@dataclass
class Order:
    order_id: str
    deadline_day: int
    budget_usd: float
    parts: list[Part]
    assembly_days: int = 1
    injected_events: list[dict[str, Any]] = field(default_factory=list)

@dataclass
class RFQ:
    rfq_id: str
    order_id: str
    kind: RFQKind
    part_ids: list[str]
    specs: dict[str, Any]
    need_by_day: int

@dataclass
class Bid:
    bid_id: str
    rfq_id: str
    supplier_id: str
    price_usd: float
    lead_days: int
    terms: str
    valid_until_day: int

@dataclass
class Reservation:
    reservation_id: str
    bid_id: str
    supplier_id: str
    confirmed: bool
    reason: str
    eta_day: int | None

@dataclass
class StatusUpdate:
    day: int
    reservation_id: str
    supplier_id: str
    status: Status
    eta_day: int | None
    note: str

@dataclass
class Inspection:
    reservation_id: str
    part_id: str
    passed: bool
    measured: dict[str, Any]
    evidence: str

@dataclass
class SupplierProfile:
    supplier_id: str
    kind: str
    materials: list[str]
    advertised_on_time_rate: float
    notes: str
