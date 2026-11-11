"""Seeded supplier simulation; NOT physical production or an independent checker.

Catalog prices must be explicit simulation assumptions. No benchmark files are
opened here; World receives its Order from an external runner. Agents see only
public Tools results and emitted events, never this ledger or future scripts.
"""
from __future__ import annotations
from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
from random import Random
from schemas import Bid, Inspection, Order, Part, Reservation, RFQ, StatusUpdate
from suppliers.base import number, validate_catalog
from suppliers.rule_based import RuleBasedSupplier
from tools_api import ToolError


class World:
    def __init__(self, order: Order, catalog_path: str, seed: int, unreliability: float, *, job_registry=None):
        config=json.loads(Path(catalog_path).read_text())
        self._initialize(order,config,seed,unreliability,job_registry,Path(catalog_path).resolve().parent)

    @classmethod
    def from_config(cls, order: Order, config: dict, seed: int, unreliability: float, *, job_registry=None, estimates_root=None):
        world=cls.__new__(cls)
        world._initialize(order,config,seed,unreliability,job_registry,Path(estimates_root) if estimates_root else Path.cwd())
        return world

    def _initialize(self,order,config,seed,unreliability,job_registry,estimates_root):
        if type(seed) is not int:raise ValueError('seed must be an integer')
        number(unreliability,'unreliability',maximum=1)
        if (not order.parts or len({p.part_id for p in order.parts})!=len(order.parts)
                or any(type(p.qty) is not int or p.qty<1 for p in order.parts)
                or type(order.assembly_days) is not int or order.assembly_days<0):
            raise ValueError('Invalid order parts or assembly days')
        self.order=deepcopy(order);self.day=0;self.seed=seed;self.unreliability=unreliability
        self.rng=Random(seed);self._catalog=validate_catalog(config)
        self.catalog_sha256=hashlib.sha256(json.dumps(self._catalog,sort_keys=True,allow_nan=False).encode()).hexdigest()
        self.suppliers={};self._supplier_config={}
        for entry in self._catalog['suppliers']:
            sid=entry['supplier_id'];self._supplier_config[sid]=deepcopy(entry)
            if entry.get('adapter')=='factoryos_pla':
                from suppliers.pla_farm import PLAFarm
                supplier=PLAFarm(sid,job_registry=job_registry,print_capacity=entry['print_capacity'])
                # Only advertised information is public. Actual outcomes stay here.
                supplier.profile.advertised_on_time_rate=entry['advertised_on_time_rate']
            else:supplier=RuleBasedSupplier(entry,estimates_root=estimates_root)
            self.suppliers[sid]=supplier
        self._records={};self._events=[];self._inspections=[];self._queued=[]
        self._paid=0.0;self._fees=0.0
        self._good={p.part_id:0 for p in order.parts};self._arrivals={};self._ready=None;self._delivery=None
        self._unit_deliveries=[];self._attempts={}
        self._scripts=[]
        for raw in order.injected_events:
            event=deepcopy(raw)
            if event.get('type') not in ('delay','inspection_fail'):raise ValueError('Unsupported injected event type')
            if event.get('type')=='delay':
                if type(event.get('day')) is not int or event['day']<0:raise ValueError('Scripted delay needs nonnegative day')
                if type(event.get('days')) is not int or event['days']<1:raise ValueError('Scripted delay needs positive days')
            elif 'day' in event and (type(event['day']) is not int or event['day']<0):raise ValueError('Invalid scripted inspection day')
            if not any(k in event for k in ('part_id','supplier_kind','reservation_id')):raise ValueError('Injected event needs a target')
            self._scripts.append({'event':event,'used':False,'applied_to':None})

    def _draw(self,key,operation):
        # Common random numbers are keyed by supplier/work/attempt, never model-
        # invented RFQ IDs. Every draw still uses the owned world.rng. Restoring
        # its quote-stream state prevents quote counts from changing failures.
        state=self.rng.getstate()
        payload=json.dumps([self.seed,key],sort_keys=True,separators=(',',':')).encode()
        self.rng.seed(int.from_bytes(hashlib.sha256(payload).digest(),'big'))
        try:return operation(self.rng)
        finally:self.rng.setstate(state)

    def register_reservation(self,reservation: Reservation,bid: Bid,rfq: RFQ) -> None:
        rid=reservation.reservation_id
        if rid in self._records:
            old=self._records[rid]
            if (old['reservation'],old['bid'],old['rfq'])!=(reservation,bid,rfq):raise ToolError('Conflicting registration for same ID')
            return
        if (not reservation.confirmed or reservation.bid_id!=bid.bid_id or rfq.rfq_id!=bid.rfq_id
                or reservation.supplier_id!=bid.supplier_id or bid.supplier_id not in self.suppliers
                or rfq.order_id!=self.order.order_id or type(reservation.eta_day) is not int
                or reservation.eta_day<self.day):raise ToolError('Invalid reservation registration')
        number(bid.price_usd,'bid price')
        if self.suppliers[bid.supplier_id].profile.kind!=rfq.kind:raise ToolError('Wrong supplier kind')
        self.validate_rfq(rfq)
        parts=rfq.specs.get('parts',[])
        canonical=(self.order.order_id,bid.supplier_id,tuple(sorted((p['part_id'],p['qty']) for p in parts)))
        attempt=self._attempts.get(canonical,0);self._attempts[canonical]=attempt+1
        outcome_key=[canonical,attempt]
        config=self._supplier_config[bid.supplier_id]
        probability=min(.9,1-config['actual_on_time_rate']+self.unreliability)
        delay=self._draw([outcome_key,'delay'],lambda r:r.randint(1,4) if r.random()<probability else 0)
        self._records[rid]={'reservation':deepcopy(reservation),'bid':deepcopy(bid),'rfq':deepcopy(rfq),
            'booked_day':self.day,'nominal_eta':reservation.eta_day,'eta':reservation.eta_day+delay,
            'random_delay':delay,'script_delay':0,'script_announced':0,'announced':False,'cancelled':False,'delivered':False,
            'shipping_scheduled':False,'outcome_key':outcome_key}
        if rfq.kind=='print':
            self._records[rid]['print_schedule']=self.suppliers[bid.supplier_id].print_schedule(rid)
        self._paid+=bid.price_usd

    def validate_rfq(self,rfq: RFQ) -> None:
        if rfq.order_id!=self.order.order_id or not isinstance(rfq.specs,dict):raise ToolError('Unknown order or invalid RFQ specs')
        parts=rfq.specs.get('parts',[])
        if not isinstance(parts,list) or any(not isinstance(p,dict) or 'part_id' not in p for p in parts):raise ToolError('Parts must be serialized Part objects')
        requested={p.part_id:p for p in self.order.parts}
        if rfq.kind!='shipping':
            if not parts or len(rfq.part_ids)!=len(set(rfq.part_ids)) or len(parts)!=len({p['part_id'] for p in parts}) or set(rfq.part_ids)!={p['part_id'] for p in parts}:raise ToolError('Parts and RFQ IDs differ')
            if any(p['part_id'] not in requested or p['kind']!=rfq.kind for p in parts):raise ToolError('Unknown part or wrong kind')
            try:
                if any(json.loads(json.dumps(asdict(Part(**p))))!=json.loads(json.dumps(asdict(requested[p['part_id']]))) for p in parts):
                    raise ToolError('RFQ specifications differ from customer requirements')
            except (TypeError,ValueError):raise ToolError('Invalid Part encoding') from None
        elif parts or rfq.part_ids:raise ToolError('Shipping RFQ must not fabricate parts')

    def can_cancel_reservation(self,reservation_id: str) -> bool:
        record=self._records.get(reservation_id)
        return record is not None and not record['delivered'] and not record['cancelled']

    def record_cancellation(self,reservation_id: str,fee_usd: float) -> None:
        number(fee_usd,'fee')
        record=self._records[reservation_id]
        if record['cancelled']:return
        if record['delivered']:raise ToolError('Cannot cancel actual completed delivery')
        record['cancelled']=True;self._paid+=fee_usd;self._fees+=fee_usd
        self._queued.append(StatusUpdate(self.day,reservation_id,record['bid'].supplier_id,'cancelled',None,'SIMULATED cancellation'))

    def inspection_for(self,reservation_id: str) -> Inspection | None:
        return next((deepcopy(x) for x in reversed(self._inspections) if x.reservation_id==reservation_id),None)

    def _matches(self,event,record,part=None):
        if 'reservation_id' in event and event['reservation_id']!=record['reservation'].reservation_id:return False
        if 'supplier_kind' in event and event['supplier_kind']!=record['rfq'].kind:return False
        if 'part_id' in event:
            ids=[part['part_id']] if part is not None else record['rfq'].part_ids
            if event['part_id'] not in ids:return False
        return True

    def _update(self,record,status,eta,note):
        return StatusUpdate(self.day,record['reservation'].reservation_id,record['bid'].supplier_id,status,eta,note)

    def _inspection(self,record,part):
        probability=min(1,self._supplier_config[record['bid'].supplier_id]['defect_rate']+self.unreliability/2)
        good=self._draw([record['outcome_key'],'defect',part['part_id']],
                        lambda r:sum(r.random()>=probability for _ in range(part['qty'])))
        forced=False
        for script in self._scripts:
            event=script['event']
            if not script['used'] and event['type']=='inspection_fail' and self.day>=event.get('day',0) and self._matches(event,record,part):
                good=0;forced=True;script['used']=True;script['applied_to']=record['reservation'].reservation_id
                break
        tolerance=part.get('tolerance_mm')
        error=(tolerance*.25 if tolerance is not None else 0) if good==part['qty'] else max(.001,(tolerance or 0)*2)
        measured={'simulation_only':True,'requested_qty':part['qty'],'good_qty':good,
                  'qty':good,'material':part.get('material'),'sku':part.get('sku'),
                  'declared_material':part.get('material'),'declared_sku':part.get('sku'),
                  'simulated_error_mm':error,'tolerance_mm':tolerance}
        # Typed evidence for the independent checker, never physical measurements.
        nominal=part.get('max_dim_mm')
        if nominal is not None:
            measured.update(nominal_max_dim_mm=nominal,max_dim_mm=nominal+error,
                            simulated_max_dim_mm=nominal+error,
                            nominal_volume_cm3=part.get('volume_cm3'))
        if part.get('size_mm') is not None:
            measured['size_mm']=[value+error for value in part['size_mm']]
            measured['nominal_size_mm']=list(part['size_mm'])
        if part.get('layers') is not None:measured['layers']=part['layers']
        return Inspection(record['reservation'].reservation_id,part['part_id'],good==part['qty'],measured,
                          'SIMULATION ONLY: seeded per-unit defect draw'+('; scripted failure' if forced else ''))

    def advance_day(self) -> tuple[list[StatusUpdate],list[Inspection]]:
        self.day+=1;updates=self._queued;self._queued=[];inspections=[]
        # Each scripted delay applies once to the first eligible matching record,
        # ordered by actual registration, on/after its scheduled day.
        for script in self._scripts:
            event=script['event']
            if script['used'] or event['type']!='delay' or self.day<event['day']:continue
            target=next((r for r in self._records.values() if not r['cancelled'] and not r['delivered'] and self._matches(event,r)),None)
            if target is not None:
                target['eta']+=event['days']
                target['script_delay']+=event['days']
                if target['rfq'].kind!='shipping' or target['shipping_scheduled']:
                    forecast=target['nominal_eta']+target['script_delay']+(target['random_delay'] if target['announced'] else 0)
                    updates.append(self._update(target,'delayed',forecast,'SIMULATED scripted delay'))
                    target['script_announced']=target['script_delay']
                script['used']=True;script['applied_to']=target['reservation'].reservation_id
        for record in self._records.values():
            if record['cancelled'] or record['delivered'] or record['rfq'].kind=='shipping':continue
            if record['random_delay'] and not record['announced'] and self.day>=max(record['booked_day']+1,record['nominal_eta']-1):
                updates.append(self._update(record,'delayed',record['eta'],'SIMULATED seeded delay announced near promised arrival'))
                record['announced']=True
            if self.day<record['eta']:continue
            record['delivered']=True
            updates.append(self._update(record,'delivered',self.day,'SIMULATED arrival at hub'))
            for part in record['rfq'].specs.get('parts',[]):
                inspection=self._inspection(record,part);inspections.append(inspection)
                self._good[part['part_id']]+=inspection.measured['good_qty']
                self._unit_deliveries.append({'part_id':part['part_id'],'day':self.day,'good_qty':inspection.measured['good_qty'],
                                             'reservation_id':record['reservation'].reservation_id})
                wanted=next(p.qty for p in self.order.parts if p.part_id==part['part_id'])
                if self._good[part['part_id']]>=wanted:self._arrivals.setdefault(part['part_id'],self.day)
        if self._ready is None and all(self._good[p.part_id]>=p.qty for p in self.order.parts):
            self._ready=self.day+self.order.assembly_days
        for record in self._records.values():
            if record['rfq'].kind!='shipping' or record['cancelled'] or record['delivered'] or self._ready is None:continue
            if not record['shipping_scheduled']:
                record['nominal_eta']=max(self._ready,record['booked_day'])+record['bid'].lead_days
                record['eta']=record['nominal_eta']+record['random_delay']+record['script_delay']
                record['shipping_scheduled']=True
                updates.append(self._update(record,'on_track',record['nominal_eta'],'SIMULATED pickup aligned with actual parts readiness'))
            if record['script_delay']>record['script_announced']:
                forecast=record['nominal_eta']+record['script_delay']+(record['random_delay'] if record['announced'] else 0)
                updates.append(self._update(record,'delayed',forecast,'SIMULATED scripted transport delay'))
                record['script_announced']=record['script_delay']
            if record['random_delay'] and not record['announced'] and self.day>=record['nominal_eta']-1:
                updates.append(self._update(record,'delayed',record['eta'],'SIMULATED transport delay after pickup alignment'))
                record['announced']=True
            if self.day>=record['eta']:
                record['delivered']=True
                self._delivery=record['eta'] if self._delivery is None else min(self._delivery,record['eta'])
                updates.append(self._update(record,'delivered',self._delivery,'SIMULATED arrival at customer'))
        self._events.extend(asdict(x) for x in updates);self._inspections.extend(inspections)
        return deepcopy(updates),deepcopy(inspections)

    def ground_truth(self) -> dict:
        return deepcopy({'simulation_only':True,'good_units':self._good,'arrival_day':self._arrivals,
            'ready_day':self._ready,'final_delivery_day':self._delivery,'total_cost':self._paid,'fees':self._fees,
            'finished':self._delivery is not None or self.day>=self.order.deadline_day+5,
            'events':self._events,'inspections':[asdict(x) for x in self._inspections],
            'unit_deliveries':self._unit_deliveries,
            'printing_schedules':{rid:r['print_schedule'] for rid,r in self._records.items() if 'print_schedule' in r},
            'printing_windows':{sid:s.printing_windows() for sid,s in self.suppliers.items() if s.profile.kind=='print'},
            'printing_bookings':{sid:s.printing_bookings() for sid,s in self.suppliers.items() if s.profile.kind=='print'},
            'print_delay_scope':'post-production logistics; sampled delays do not invent additional machine runtime'})
