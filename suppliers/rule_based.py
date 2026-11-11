"""Deterministic rule-based quotes from explicit simulation configuration."""
from __future__ import annotations
from copy import deepcopy
import math
from random import Random
from schemas import Bid, Part, Reservation, RFQ, SupplierProfile
from suppliers.base import validate_catalog,number
from tools_api import ToolError
from pathlib import Path
import json
from suppliers.print_calendar import PrintCalendar,PrintUnavailable
from suppliers.print_estimates import SavedPrintEstimates


class RuleBasedSupplier:
    def __init__(self, config: dict, *, estimates_root: Path | None = None):
        self.config=validate_catalog({'price_origin':'simulation_assumptions','suppliers':[config]})['suppliers'][0]
        c=self.config
        self.profile=SupplierProfile(c['supplier_id'],c['kind'],list(c.get('materials',[])),
            c['advertised_on_time_rate'],'SIMULATED supplier and prices; no observed market quote')
        self.last_refusal=None;self._calendar=None;self._estimates=None;self._estimate_error=None
        self._plans={};self._plates={}
        if c['kind']=='print':
            self._calendar=PrintCalendar(c['print_capacity'])
            try:
                if not c.get('saved_estimates_file'):raise PrintUnavailable('Missing reviewed saved_estimates_file; no time can be inferred from volume')
                self._estimates=SavedPrintEstimates((estimates_root or Path.cwd())/c['saved_estimates_file'],allow_synthetic=c.get('fixture_only') is True)
            except PrintUnavailable as exc:self._estimate_error=str(exc)
            self.profile.notes+=' Printing requires reviewed saved whole-plate estimates; time is never inferred from volume.'
            if self._estimate_error:self.profile.notes+=' Quoting unavailable: '+self._estimate_error
        self._bids={};self._qty={};self._accepted={};self._cancelled=set();self._booked={}

    def _parts(self, rfq):
        if rfq.kind!=self.profile.kind:return None
        raw=rfq.specs.get('parts',[] if rfq.kind=='shipping' else None)
        if not isinstance(raw,list):return None
        try:parts=[Part(**p) for p in raw]
        except (TypeError,ValueError):return None
        if (len(set(rfq.part_ids))!=len(rfq.part_ids) or set(rfq.part_ids)!={p.part_id for p in parts}
                or len({p.part_id for p in parts})!=len(parts)):return None
        if rfq.kind=='shipping':return [] if not parts else None
        if not parts:return None
        for p in parts:
            if p.kind!=rfq.kind or type(p.qty) is not int or p.qty<1:return None
            try:
                if p.kind=='print':
                    limits=self.config['limits']
                    if p.material not in self.profile.materials:return None
                    if self.config['pricing'].get('mode','per_cm3')=='per_cm3' or p.volume_cm3 is not None:
                        number(p.volume_cm3,'volume',minimum=.000001)
                    number(p.max_dim_mm,'dimension',minimum=.000001)
                    if p.max_dim_mm>limits['max_dim_mm']:return None
                elif p.kind=='pcb':
                    limits=self.config['limits']
                    if type(p.layers) is not int or p.layers not in limits['supported_layers']:return None
                    if not p.size_mm or len(p.size_mm)!=2:return None
                    for size,limit in zip(p.size_mm,limits['max_size_mm']):
                        number(size,'PCB size',minimum=.000001)
                        if size>limit:return None
                elif p.sku not in self.config['pricing']['sku_prices_usd']:return None
                if p.kind in ('print','pcb') and p.tolerance_mm is not None:
                    number(p.tolerance_mm,'tolerance',minimum=0)
                    if p.tolerance_mm<self.config['limits']['min_tolerance_mm']:return None
            except (TypeError,ValueError):return None
        return parts

    def quote(self, rfq: RFQ, day: int, rng: Random) -> Bid | None:
        parts=self._parts(rfq)
        if parts is None:
            self.last_refusal='Requested parts do not match declared supplier capabilities';return None
        key=f'{self.profile.supplier_id}:{rfq.rfq_id}:{day}'
        plate_sets=None
        if self._calendar:
            try:
                if self._estimates is None:raise PrintUnavailable(self._estimate_error or 'Missing saved slicer estimates')
                plate_sets=self._estimates.resolve(rfq)
            except PrintUnavailable as exc:self.last_refusal=str(exc);return None
        if key in self._bids:
            bid,original=self._bids[key]
            if original!=rfq:raise ToolError('RFQ ID reused with different content')
            self.last_refusal=None;return deepcopy(bid)
        price=self.config['pricing'];kind=self.profile.kind;qty=sum(p.qty for p in parts) or 1
        if kind=='print':
            choices=[]
            for plates in plate_sets:
                try:choices.append((self._calendar.preview(list(plates),day),plates))
                except PrintUnavailable:pass
            if not choices:
                self.last_refusal='No matching printer can fit the entire sliced plate plus setup in a continuous operating window';return None
            plan,plates=min(choices,key=lambda c:(c[0].arrival_hour,sum(p.machine_seconds for p in c[1]),tuple(p.plate_id for p in c[1])))
            self._plans[key]=plan;self._plates[key]=plates
            mode=price.get('mode','per_cm3')
            if mode=='time':
                base=price['setup_usd']+price['material_usd_per_g']*sum(float(p.filament_g) for p in plates)+price['machine_usd_per_hour']*sum(p.machine_seconds for p in plates)/3600
            elif mode=='per_gram_inclusive':base=price['setup_usd']+price['per_gram_usd']*sum(float(p.filament_g) for p in plates)
            else:base=price['setup_usd']+price['per_cm3_usd']*sum(p.volume_cm3*p.qty for p in parts)
            lead=plan.eta_day-day
            terms=json.dumps({'scope':'SIMULATION; no physical production','pricing_mode':mode,
                              'plate_quantity_already_included':True,'schedule':plan.as_dict()},sort_keys=True)
        else:
            if kind=='pcb':base=price['setup_usd']+price.get('per_board_usd',0)*qty+price.get('per_cm2_layer_usd',0)*sum(math.prod(p.size_mm)/100*p.layers*p.qty for p in parts)
            elif kind=='purchased':base=price['handling_usd']+sum(price['sku_prices_usd'][p.sku]*p.qty for p in parts)
            else:base=price['flat_usd']
            cap=self.config['capacity_per_day']
            pending=sum(self._qty[r.bid_id] for rid,(r,start) in self._accepted.items() if rid not in self._cancelled and r.eta_day>day)
            lead=self.config['lead_days']+(pending//cap if cap else 0)
            terms='SIMULATION ASSUMPTIONS; paid at award; cancellation adds 50% fee'
        factor=1.0
        for threshold,discount in sorted(self.config.get('discount_tiers',[])):
            if qty>=threshold:factor=discount
        jitter=self.config.get('price_jitter_fraction',.1)
        amount=round(base*factor*rng.uniform(1-jitter,1+jitter),2)
        bid=Bid(key,rfq.rfq_id,self.profile.supplier_id,amount,lead,terms,day+2)
        self._bids[key]=(deepcopy(bid),deepcopy(rfq));self._qty[key]=qty
        self.last_refusal=None
        return bid

    def accept(self, bid: Bid, day: int) -> Reservation:
        rid='reservation:'+bid.bid_id
        reason=''
        if rid in self._cancelled:reason='Already cancelled'
        elif rid in self._accepted:return deepcopy(self._accepted[rid][0])
        elif bid.bid_id not in self._bids or self._bids[bid.bid_id][0]!=bid:reason='Unknown or changed bid'
        elif day>bid.valid_until_day:reason='Expired bid'
        elif self._calendar:
            try:
                if self._plates[bid.bid_id] not in self._estimates.resolve(self._bids[bid.bid_id][1]):raise PrintUnavailable('Reviewed plate estimates changed')
                self._calendar.reserve(self._plans[bid.bid_id],rid,day)
            except PrintUnavailable as exc:reason=str(exc)
        elif self.config['capacity_per_day'] is not None and self._booked.get(day,0)+self._qty[bid.bid_id]>self.config['capacity_per_day']:reason='Daily intake capacity full'
        eta=(self._plans[bid.bid_id].eta_day if self._calendar else day+bid.lead_days) if not reason else None
        result=Reservation(rid,bid.bid_id,self.profile.supplier_id,not reason,reason or 'SIMULATED production only',eta)
        if result.confirmed:
            self._accepted[rid]=(deepcopy(result),day)
            if not self._calendar:self._booked[day]=self._booked.get(day,0)+self._qty[bid.bid_id]
        return result

    def cancel(self, reservation_id: str, day: int) -> float:
        if reservation_id in self._cancelled:return 0.0
        if reservation_id not in self._accepted:raise ToolError('Unknown reservation')
        r,start=self._accepted[reservation_id]
        if self._calendar:self._calendar.cancel(reservation_id,day)
        else:self._booked[start]-=self._qty[r.bid_id]
        self._cancelled.add(reservation_id)
        return self._bids[r.bid_id][0].price_usd*.5

    def print_schedule(self,reservation_id: str):
        if not self._calendar or reservation_id not in self._accepted:return None
        bid_id=self._accepted[reservation_id][0].bid_id
        return self._plans[bid_id].as_dict()

    def printing_windows(self):
        return self._calendar.describe() if self._calendar else None

    def printing_bookings(self):
        return self._calendar.snapshot() if self._calendar else None
