"""Audited Tools implementation. Never exposes simulator truth to agents."""
from __future__ import annotations
from copy import deepcopy
from dataclasses import asdict
import json
import math
from tools_api import MAX_TOOL_CALLS, ToolBudgetExceeded, ToolError, ToolNotAllowed
from marketplace.world_bridge import bridge_for
from schemas import RFQ, Bid, Reservation, Inspection, SupplierProfile
from tools_api import World


class Marketplace:
    def __init__(self, world: World, allow_broadcast: bool = True, *, bridge=None, observer=None):
        self._world = world
        self.allow_broadcast = allow_broadcast
        self._bridge = bridge if bridge is not None else bridge_for(world)
        self._observer=observer
        self.observer_errors=0
        self._calls = self._attempts = 0
        self._records = []
        self._rfqs = {}
        self._bids = {}
        self._reservations = {}
        self._cancelled = set()

    def _call(self, name, arguments, operation):
        self._attempts += 1
        record = {'day': self._world.day, 'tool': name, 'arguments': deepcopy(arguments)}
        if self._calls >= MAX_TOOL_CALLS:
            record['error'] = 'ToolBudgetExceeded'
            self._records.append(record)
            self._observe(record)
            raise ToolBudgetExceeded('60 counted calls exhausted; operation not executed')
        self._calls += 1
        try:
            result = operation()
            record['result'] = json.loads(json.dumps(result, default=asdict, allow_nan=False))
            if name == 'request_quote' and result is None:
                record['refusal_reason'] = getattr(self._world.suppliers.get(arguments['supplier_id']), 'last_refusal', None)
            elif name == 'post_rfq':
                record['supplier_refusals'] = {sid:s.last_refusal for sid,s in self._world.suppliers.items()
                    if s.profile.kind == arguments['rfq']['kind'] and getattr(s,'last_refusal',None)}
            return deepcopy(result)
        except Exception as exc:
            record['error'] = type(exc).__name__
            raise
        finally:
            self._records.append(record)
            self._observe(record)

    def _observe(self,record):
        if self._observer is not None:
            try:self._observer(deepcopy(record))
            except Exception:self.observer_errors+=1  # observer failure must not change agent behavior

    def today(self) -> int:
        return self._call('today', {}, lambda: self._world.day)

    def list_suppliers(self, kind: str | None = None) -> list[SupplierProfile]:
        return self._call('list_suppliers', {'kind': kind}, lambda: [
            s.profile for _, s in sorted(self._world.suppliers.items())
            if kind is None or s.profile.kind == kind])

    def _quote(self, supplier_id: str, rfq: RFQ) -> Bid | None:
        if rfq.kind not in ('print', 'pcb', 'purchased', 'shipping'):
            raise ToolError('Invalid RFQ kind')
        if rfq.rfq_id in self._rfqs and self._rfqs[rfq.rfq_id] != rfq:
            raise ToolError('RFQ ID reused with different content')
        if supplier_id not in self._world.suppliers:
            raise ToolError('Unknown supplier ID; discover suppliers first')
        validator = getattr(self._bridge, 'validate_rfq', None)
        if validator is not None:
            validator(rfq)
        self._rfqs[rfq.rfq_id] = deepcopy(rfq)
        bid = self._world.suppliers[supplier_id].quote(deepcopy(rfq), self._world.day, self._bridge.rng)
        if bid is not None:
            if (bid.supplier_id != supplier_id or bid.rfq_id != rfq.rfq_id
                    or not bid.bid_id or not math.isfinite(bid.price_usd) or bid.price_usd < 0
                    or type(bid.lead_days) is not int or bid.lead_days < 0
                    or type(bid.valid_until_day) is not int):
                raise ToolError('Supplier returned an invalid bid')
            if bid.bid_id in self._bids and self._bids[bid.bid_id] != bid:
                raise ToolError('Supplier reused bid ID with different content')
            self._bids[bid.bid_id] = deepcopy(bid)
        return bid

    def post_rfq(self, rfq: RFQ) -> list[Bid]:
        def operation():
            if not self.allow_broadcast:
                raise ToolNotAllowed('Broadcast disabled for direct-supplier condition')
            return [b for sid, supplier in sorted(self._world.suppliers.items())
                    if supplier.profile.kind == rfq.kind and (b := self._quote(sid, rfq)) is not None]
        return self._call('post_rfq', {'rfq': asdict(rfq)}, operation)

    def request_quote(self, supplier_id: str, rfq: RFQ) -> Bid | None:
        return self._call('request_quote', {'supplier_id': supplier_id, 'rfq': asdict(rfq)},
                          lambda: self._quote(supplier_id, rfq))

    def award(self, bid_id: str) -> Reservation:
        def operation():
            if bid_id not in self._bids:
                raise ToolError('Unknown bid ID; use an actual quote result')
            if bid_id in self._reservations:
                existing = self._reservations[bid_id]
                if existing.reservation_id in self._cancelled:
                    raise ToolError('Cancelled bid cannot be awarded again')
                return existing
            bid = self._bids[bid_id]
            if self._world.day > bid.valid_until_day:
                raise ToolError('Expired bid; obtain a new quote')
            result = self._world.suppliers[bid.supplier_id].accept(deepcopy(bid), self._world.day)
            if result.bid_id != bid_id or result.supplier_id != bid.supplier_id:
                raise RuntimeError('Supplier violated reservation contract; abort run')
            if result.confirmed:
                if not result.reservation_id or type(result.eta_day) is not int or result.eta_day < self._world.day:
                    raise RuntimeError('Supplier confirmed invalid reservation ID/ETA; abort run')
                try:
                    self._bridge.register(result, bid, self._rfqs[bid.rfq_id])
                except Exception:
                    raise RuntimeError('World settlement failed after supplier acceptance; abort run') from None
                self._reservations[bid_id] = deepcopy(result)
            return result
        return self._call('award', {'bid_id': bid_id}, operation)

    def cancel(self, reservation_id: str) -> dict:
        def operation():
            if reservation_id in self._cancelled:
                return {'ok': True, 'fee_usd': 0.0}
            reservation = next((r for r in self._reservations.values()
                                if r.reservation_id == reservation_id), None)
            if reservation is None:
                raise ToolError('Unknown reservation')
            if not self._bridge.can_cancel(reservation_id):
                return {'ok': False, 'fee_usd': 0.0}
            fee = self._world.suppliers[reservation.supplier_id].cancel(reservation_id, self._world.day)
            if not math.isfinite(fee) or fee < 0:
                raise RuntimeError('Supplier returned invalid fee after cancellation; abort run')
            try:
                self._bridge.cancel(reservation_id, fee)
            except Exception:
                raise RuntimeError('World settlement failed after cancellation; abort run') from None
            self._cancelled.add(reservation_id)
            return {'ok': True, 'fee_usd': fee}
        return self._call('cancel', {'reservation_id': reservation_id}, operation)

    def get_inspection(self, reservation_id: str) -> Inspection | None:
        return self._call('get_inspection', {'reservation_id': reservation_id},
                          lambda: self._bridge.inspection(reservation_id))

    def log(self, msg: str) -> None:
        return self._call('log', {'msg': msg}, lambda: None)

    def usage(self) -> dict:
        return {'tool_calls': self._calls, 'attempted_calls': self._attempts, 'max_tool_calls': MAX_TOOL_CALLS}

    def trace(self) -> list[dict]:
        return deepcopy(self._records)
