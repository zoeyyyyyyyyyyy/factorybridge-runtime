"""Stateful RFQ planner. LLM reviews real feasible choices; Python checks awards.

No simulator/checker access, synthetic answers, or silent no-LLM fallback.
"""
from __future__ import annotations
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from heapq import nsmallest
from itertools import product
import json
import math
import llm
from agents.common import SYSTEM_PROMPT, public_order
from schemas import Bid, Inspection, Order, Part, Reservation, RFQ, StatusUpdate
from tools_api import Tools, ToolError

SELECT_PLAN = [{'type': 'function', 'function': {
    'name': 'select_plan', 'description': 'Select a supplied feasible plan ID, or refuse with a reason.',
    'parameters': {'type': 'object', 'additionalProperties': False,
                   'properties': {'plan_id': {'type': 'string'}, 'reason': {'type': 'string'}},
                   'required': ['plan_id', 'reason']}}}]


@dataclass
class Package:
    key: str
    kind: str
    parts: list[Part] = field(default_factory=list)
    bid: Bid | None = None
    reservation: Reservation | None = None
    eta: int | None = None
    status: str = 'unreserved'
    inspections: dict[str, Inspection] = field(default_factory=dict)
    shipping_delay: int = 0

    def observed_good(self):
        return self.status == 'delivered' and all(
            p.part_id in self.inspections and self.inspections[p.part_id].passed for p in self.parts)


class Planner:
    name = 'planner'

    def __init__(self):
        self.order: Order | None = None
        self.packages: dict[str, Package] = {}
        self.paid_usd = 0.0
        self.fees_usd = 0.0
        self.revision = 0
        self.decisions = []
        self.messages = []  # Actual per-invocation prompts/replies, not a fabricated conversation.
        self.refusal = None
        self._denied = set()
        self._refusals = {}  # (day, package, supplier) -> refused awards that day

    def start(self, order: Order, tools: Tools) -> None:
        if self.order is not None:
            raise ValueError('Use a new Planner for every order/run')
        if (not order.parts or len({p.part_id for p in order.parts}) != len(order.parts)
                or any(type(p.qty) is not int or p.qty < 1 for p in order.parts)
                or not math.isfinite(order.budget_usd) or order.budget_usd < 0
                or order.deadline_day < 0 or order.assembly_days < 0):
            raise ValueError('Invalid order quantities, IDs, budget or schedule')
        self.order = deepcopy(order)
        purchased = []
        for index, part in enumerate(order.parts):
            if part.kind == 'purchased':
                purchased.append(deepcopy(part))
            elif part.kind in ('print', 'pcb'):
                key = f'part-{index}'
                self.packages[key] = Package(key, part.kind, [deepcopy(part)])
            else:
                raise ValueError('Unsupported part kind')
        if purchased:
            self.packages['purchased'] = Package('purchased', 'purchased', purchased)
        self.packages['shipping'] = Package('shipping', 'shipping')
        day = tools.today()
        self._plan(day, list(self.packages), tools)

    def snapshot(self):
        return {'order_id': self.order.order_id if self.order else None,
                'paid_usd': self.paid_usd, 'fees_usd': self.fees_usd,
                'refusal': self.refusal,
                'customer_delivery_observed': self.packages.get('shipping') is not None
                    and self.packages['shipping'].status == 'delivered',
                'all_part_inspections_passed': bool(self.packages) and all(
                    p.observed_good() for p in self.packages.values() if p.kind != 'shipping'),
                'packages': {key: {'kind': p.kind, 'part_ids': [q.part_id for q in p.parts],
                    'reservation_id': p.reservation.reservation_id if p.reservation else None,
                    'bid_id': p.bid.bid_id if p.bid else None, 'status': p.status, 'eta_day': p.eta,
                    'inspections': {k: {'passed': v.passed, 'evidence': v.evidence} for k,v in p.inspections.items()}}
                    for key, p in self.packages.items()}}

    def _log(self, tools, event, **details):
        record = {'agent': self.name, 'event': event, **details}
        self.decisions.append(deepcopy(record))
        tools.log(json.dumps(record, allow_nan=False))

    def _quotes(self, key, day, need, tools):
        p = self.packages[key]
        self.revision += 1
        rfq = RFQ(f'{self.order.order_id}:planner:{key}:{self.revision}', self.order.order_id,
                  p.kind, [x.part_id for x in p.parts], {'parts': [asdict(x) for x in p.parts]}, need)
        return sorted([b for b in tools.post_rfq(rfq)
                       if (day, key, b.supplier_id) not in self._denied
                       and math.isfinite(b.price_usd) and b.price_usd >= 0
                       and type(b.lead_days) is int and b.lead_days >= 0
                       and b.valid_until_day >= day],
                      key=lambda b: (b.price_usd, b.lead_days, b.supplier_id, b.bid_id))

    def _options(self, day, keys, tools):
        ship = self.packages['shipping']
        ships = self._quotes('shipping', day, self.order.deadline_day, tools) if 'shipping' in keys else [ship.bid]
        if 'shipping' in keys and ship.bid and ship.status not in ('cancelled', 'failed', 'delivered'):
            ships.insert(0, ship.bid)
        options = []
        cache = {}
        combinations_checked = 0

        def keeping(key, bid):
            p = self.packages[key]
            return p.bid is not None and p.bid.bid_id == bid.bid_id and p.status not in ('cancelled', 'failed')

        for shipping in ships:
            if shipping is None:
                continue
            ship_lead = shipping.lead_days + (ship.shipping_delay if keeping('shipping', shipping) else 0)
            need = self.order.deadline_day - self.order.assembly_days - ship_lead
            fixed_eta = [p.eta if p.observed_good() else max(day, p.eta if p.eta is not None else day)
                         for k,p in self.packages.items() if k not in keys and p.kind != 'shipping']
            if fixed_eta and max(fixed_eta) > need:
                continue
            # Suppliers receive the actual backward-scheduled hub deadline, not
            # a looser deadline that could hide an available express quote.
            if need not in cache:
                candidates = {key: self._quotes(key, day, need, tools) for key in keys if key != 'shipping'}
                for key, bids in candidates.items():
                    p = self.packages[key]
                    if (p.bid and p.status in ('on_track', 'delayed') and p.eta is not None
                            and all(i.passed for i in p.inspections.values())):
                        bids.insert(0, p.bid)
                cache[need] = candidates
            candidates = cache[need]
            if any(not b for b in candidates.values()):
                continue
            combinations_checked += math.prod(len(b) for b in candidates.values())
            if combinations_checked > 100_000:
                raise ToolError('More than 100000 bid combinations; split order before running')
            names = list(candidates)
            for combination in product(*(candidates[k] for k in names)):
                etas = [max(day, self.packages[k].eta) if keeping(k,b) else day+b.lead_days
                        for k,b in zip(names,combination)]
                if any(eta > need for eta in etas):
                    continue
                chosen = {k:b for k,b in zip(names,combination) if not keeping(k,b)}
                if 'shipping' in keys and not keeping('shipping',shipping):
                    chosen['shipping'] = shipping
                cancellation = sum(self.packages[k].bid.price_usd * .5 for k in chosen
                    if self.packages[k].reservation and self.packages[k].status not in ('cancelled', 'failed', 'delivered'))
                total = self.paid_usd + cancellation + sum(b.price_usd for b in chosen.values())
                if total > self.order.budget_usd + 1e-9:
                    continue
                latest = max([*fixed_eta, *etas], default=day)
                arrival = max(day, latest + self.order.assembly_days) + ship_lead
                if arrival <= self.order.deadline_day:
                    options.append({'bids': chosen, 'projected_total_usd': round(total, 8),
                                    'predicted_delivery_day': arrival, 'parts_need_by_day': need,
                                    'estimated_cancellation_usd': cancellation})
        return nsmallest(8, options, key=lambda o: (o['projected_total_usd'], o['predicted_delivery_day'],
                         tuple(b.bid_id for b in o['bids'].values())))

    def _review(self, day, options):
        table = [{'plan_id': f'plan-{i}', **{k:v for k,v in o.items() if k != 'bids'},
                  'bid_ids': {k: b.bid_id for k,b in o['bids'].items()}} for i,o in enumerate(options)]
        bid_table = {b.bid_id: asdict(b) for o in options for b in o['bids'].values()}
        messages = [{'role': 'system', 'content': SYSTEM_PROMPT + '\nYou review marketplace plans validated against current quotes. '
                     'Choose the lowest-cost feasible supplied plan (break ties by earlier delivery), '
                     'or select plan_id=refuse with a concrete reason. Do not invent IDs. '
                     'Projected dates are estimates, never evidence of completed delivery.'},
                    {'role': 'user', 'content': json.dumps({'day': day, 'order': public_order(self.order),
                        'state': self.snapshot(), 'bid_table': bid_table, 'feasible_plans': table}, allow_nan=False)}]
        # Two bounded attempts to repair serialization, never silently replace the model.
        for attempt in range(2):
            reply = llm.call_llm(messages, SELECT_PLAN)
            self.messages.append({'request': deepcopy(messages), 'response': deepcopy(reply)})
            try:
                calls = reply.get('tool_calls') or []
                if calls:
                    if len(calls) != 1 or calls[0]['function']['name'] != 'select_plan':
                        raise ValueError('Expected one select_plan call')
                    selection = json.loads(calls[0]['function']['arguments'])
                else:
                    selection = json.loads(reply.get('content') or '')
                if not isinstance(selection.get('reason'), str) or not selection['reason'].strip():
                    raise ValueError('A reason is required')
                ids = {f'plan-{i}': i for i in range(len(options))}
                if selection.get('plan_id') == 'refuse':
                    return None, selection['reason']
                if selection.get('plan_id') not in ids:
                    raise ValueError('Unknown plan ID')
                return options[ids[selection['plan_id']]], selection['reason']
            except (ValueError, TypeError, KeyError, AttributeError):
                if attempt == 1:
                    raise ToolError('Model returned no valid supplied plan after two attempts') from None
                # Repair prompt includes no fabricated tool result or unmatched tool call.
                messages = messages[:2] + [{'role': 'user', 'content': 'Invalid selection encoding. '
                    'Return exactly one select_plan tool call with an ID from feasible_plans and a reason.'}]

    def _plan(self, day, keys, tools):
        # Capacity fallback: a refused award (e.g. a printer slot taken by a sibling
        # package awarded moments earlier) is re-quoted from the SAME supplier first,
        # because a fresh quote reflects the updated calendar. Only a second refusal
        # the same day excludes that supplier for that package.
        for retry in range(3):
            if not keys:
                return
            options = self._options(day, keys, tools)
            if not options:
                self.refusal = 'No quoted combination fits remaining budget and deadline'
                self._log(tools, 'no_feasible_replan', day=day, packages=keys, reason=self.refusal)
                return
            selected, reason = self._review(day, options)
            if selected is None:
                self.refusal = reason
                self._log(tools, 'model_refusal', day=day, reason=reason)
                return
            self._log(tools, 'selected_plan', day=day, reason=reason,
                      projected_total_usd=selected['projected_total_usd'],
                      predicted_delivery_day=selected['predicted_delivery_day'],
                      bid_ids={k:b.bid_id for k,b in selected['bids'].items()})
            failed = []
            for key, bid in selected['bids'].items():
                p = self.packages[key]
                if p.reservation and p.status not in ('cancelled', 'failed', 'delivered'):
                    result = tools.cancel(p.reservation.reservation_id)
                    if not result['ok']:
                        self.refusal = 'Cancellation refused; existing reservation retained'
                        self._log(tools, 'replacement_blocked', package=key, reason=self.refusal)
                        return
                    self.paid_usd += result['fee_usd']
                    self.fees_usd += result['fee_usd']
                    p.status = 'cancelled'
                # Recheck actual fees before committing; cancellation can differ by adapter.
                if self.paid_usd + bid.price_usd > self.order.budget_usd + 1e-9:
                    self.refusal = 'Actual cancellation fee exhausted remaining budget'
                    self._log(tools, 'replacement_blocked', package=key, reason=self.refusal)
                    return
                reservation = tools.award(bid.bid_id)
                # Earlier awards may occupy slots that every preview saw as
                # free. Refresh that same offer once before excluding a usable
                # supplier. Only equal/better price and promised arrival retain
                # the already reviewed plan; changed terms use normal replanning.
                if (not reservation.confirmed and p.kind == 'print'
                        and 'request a new quote' in reservation.reason.lower()):
                    self.revision += 1
                    rfq = RFQ(f'{self.order.order_id}:planner:{key}:{self.revision}',
                        self.order.order_id, p.kind, [x.part_id for x in p.parts],
                        {'parts': [asdict(x) for x in p.parts]}, selected['parts_need_by_day'])
                    fresh = tools.request_quote(bid.supplier_id, rfq)
                    if (fresh is not None and fresh.supplier_id == bid.supplier_id
                            and math.isfinite(fresh.price_usd) and 0 <= fresh.price_usd <= bid.price_usd
                            and type(fresh.lead_days) is int and 0 <= fresh.lead_days <= bid.lead_days
                            and fresh.valid_until_day >= day):
                        bid = fresh
                        reservation = tools.award(bid.bid_id)
                        self._log(tools, 'booking_quote_refreshed', package=key,
                                  confirmed=reservation.confirmed, supplier_id=bid.supplier_id,
                                  reason='Same selected supplier with no worse price or hub promise')
                if not reservation.confirmed:
                    refusal_key = (day, key, bid.supplier_id)
                    self._refusals[refusal_key] = self._refusals.get(refusal_key, 0) + 1
                    if self._refusals[refusal_key] >= 2:
                        self._denied.add(refusal_key)
                    failed.append(key)
                    self._log(tools, 'capacity_refusal', package=key, reason=reservation.reason)
                    continue
                self.paid_usd += bid.price_usd
                p.bid, p.reservation = deepcopy(bid), deepcopy(reservation)
                p.eta, p.status, p.inspections, p.shipping_delay = reservation.eta_day, 'on_track', {}, 0
            keys = failed
            if not keys:
                self.refusal = None
                return
            # A later re-quoted slot can push the critical path; let the fallback
            # also reconsider customer shipping (the current booking stays an option).
            ship = self.packages['shipping']
            if 'shipping' not in keys and ship.status != 'delivered':
                keys = [*keys, 'shipping']
        if keys:
            self.refusal = 'Capacity fallback exhausted; outstanding packages remain'
            self._log(tools, 'incomplete_plan', packages=keys, reason=self.refusal)

    def on_events(self, day: int, updates: list[StatusUpdate], inspections: list[Inspection], tools: Tools) -> None:
        active = {p.reservation.reservation_id: p for p in self.packages.values() if p.reservation}
        failed = set()
        for update in updates:
            p = active.get(update.reservation_id)
            if p is None or update.supplier_id != p.reservation.supplier_id:
                continue
            if p.status == 'delivered':
                continue
            if update.status == 'delayed' and p.kind == 'shipping' and update.eta_day is not None:
                p.shipping_delay += max(0, update.eta_day-(p.eta if p.eta is not None else update.eta_day))
            p.status = update.status
            if update.eta_day is not None:
                p.eta = update.eta_day
            if p.status in ('failed', 'cancelled'):
                failed.add(p.key)
        for inspection in inspections:
            p = active.get(inspection.reservation_id)
            if p is not None and inspection.part_id in {q.part_id for q in p.parts}:
                p.inspections[inspection.part_id] = deepcopy(inspection)
                if not inspection.passed:
                    failed.add(p.key)
        ship = self.packages['shipping']
        if ship.status == 'delivered':
            self._log(tools, 'observed_customer_delivery', day=day, state=self.snapshot())
            return
        if not ship.reservation or ship.status in ('failed', 'cancelled'):
            failed.add('shipping')
        lead = ship.bid.lead_days + ship.shipping_delay if ship.bid else 0
        need = self.order.deadline_day-self.order.assembly_days-lead
        for key,p in self.packages.items():
            if p.kind == 'shipping' or p.observed_good():
                continue
            if p.reservation is None or (p.eta is not None and p.eta > need):
                failed.add(key)
        if ship.shipping_delay and not failed:
            latest = max([day, *(p.eta or day for p in self.packages.values() if p.kind != 'shipping' and not p.observed_good())])
            if latest+self.order.assembly_days+lead > self.order.deadline_day:
                failed.add('shipping')
        if failed and ship.reservation and ship.status not in ('delivered', 'cancelled', 'failed'):
            # Reconsider transport as well as fabrication when the critical path slips.
            failed.add('shipping')
        if failed:
            self._log(tools, 'critical_path_at_risk', day=day, packages=sorted(failed))
            self._plan(day, sorted(failed), tools)
        else:
            self._log(tools, 'retain_plan', day=day, state=self.snapshot())
