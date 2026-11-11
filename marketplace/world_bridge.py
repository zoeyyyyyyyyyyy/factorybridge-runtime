"""Explicit settlement hooks absent from the frozen World protocol.

Only P3's known fake World gets an automatic compatibility adapter. P2 supplies
callbacks for the real simulator, without exposing its truth or changing APIs.
"""
from dataclasses import dataclass
from random import Random
from typing import Callable
from schemas import Bid, RFQ, Reservation, Inspection


@dataclass
class CallbackBridge:
    rng: Random
    register: Callable[[Reservation, Bid, RFQ], None]
    cancel: Callable[[str, float], None]
    inspection: Callable[[str], Inspection | None]
    can_cancel: Callable[[str], bool]
    validate_rfq: Callable[[RFQ], None] | None = None


def bridge_for(world):
    if type(world).__module__ == 'tests.fake_world' and type(world).__name__ == 'World':
        from copy import deepcopy
        return CallbackBridge(
            world.rng, world._register, world._cancel,
            lambda rid: next((deepcopy(i) for i in reversed(world._inspections)
                              if i.reservation_id == rid), None),
            lambda rid: rid in world._records and not world._records[rid]['delivered'])
    if type(world).__module__ == 'simulator' and type(world).__name__ == 'World':
        return CallbackBridge(world.rng, world.register_reservation, world.record_cancellation,
                              world.inspection_for, world.can_cancel_reservation, world.validate_rfq)
    raise TypeError('World settlement API missing from frozen protocol; pass '
                    'bridge=CallbackBridge(...). See docs/PLANNER_INTEGRATION.md')
