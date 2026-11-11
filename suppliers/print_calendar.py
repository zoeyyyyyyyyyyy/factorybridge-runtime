"""Per-printer non-preemptive calendars. Internal units are exact machine hours."""
from __future__ import annotations
from copy import deepcopy
from dataclasses import dataclass
from fractions import Fraction
import math
import threading


class PrintUnavailable(ValueError):
    pass


def hours(value, name, *, positive=False):
    if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or value<0 or (positive and value==0):
        raise ValueError(f'{name} must be finite and '+('positive' if positive else 'nonnegative'))
    return Fraction(str(value))


def arrival_day(hour: Fraction) -> int:
    """Day d is boundary 24*d; an arrival at exactly hour 24 is day 1."""
    return math.ceil(hour/24)


@dataclass(frozen=True)
class Plate:
    plate_id: str
    machine_seconds: int
    filament_g: str
    material: str
    profile_sha256: str
    settings_sha256: str
    evidence_id: str
    origin: str


@dataclass(frozen=True)
class Slot:
    printer_id: str
    plate: Plate
    start: Fraction
    finish: Fraction


@dataclass(frozen=True)
class PrintPlan:
    slots: tuple[Slot,...]
    arrival_hour: Fraction

    @property
    def eta_day(self):
        return arrival_day(self.arrival_hour)

    def as_dict(self):
        return {'arrival_hour':float(self.arrival_hour),'arrival_day':self.eta_day,
                'slots':[{'printer_id':s.printer_id,'plate_id':s.plate.plate_id,
                    'start_hour':float(s.start),'finish_hour':float(s.finish),
                    'machine_seconds':s.plate.machine_seconds,'filament_g':s.plate.filament_g,
                    'evidence_id':s.plate.evidence_id,'estimate_origin':s.plate.origin,
                    'profile_sha256':s.plate.profile_sha256,'settings_sha256':s.plate.settings_sha256}
                    for s in self.slots]}


class PrintCalendar:
    def __init__(self, config: dict):
        if not isinstance(config,dict):raise ValueError('print_capacity must be explicitly configured')
        printers=config.get('printers')
        if not isinstance(printers,list) or not printers:raise ValueError('Configure at least one printer')
        self.printers={}
        for raw in printers:
            p=deepcopy(raw);pid=p.get('printer_id')
            if not isinstance(pid,str) or not pid or pid in self.printers:raise ValueError('Printer IDs must be unique strings')
            width=hours(p['operating_hours_per_day'],'operating_hours_per_day',positive=True)
            start=hours(p['window_start_hour'],'window_start_hour')
            if width>24 or start>=24 or (width==24 and start!=0):raise ValueError('Use a daily window <=24h; continuous 24h requires start 0')
            if 'settings_sha256' in p and 'allowed_settings_sha256' in p:raise ValueError('Choose one settings fingerprint or an explicit allowed-settings list')
            settings=p.get('allowed_settings_sha256',[p.get('settings_sha256')])
            if not isinstance(settings,list) or not settings:raise ValueError('Printer needs allowed print-settings fingerprints')
            for value in [p.get('profile_sha256'),*settings]:
                if not isinstance(value,str) or len(value)!=64 or any(c not in '0123456789abcdef' for c in value):raise ValueError('Printer profile/settings must identify actual configuration bytes')
            self.printers[pid]={**p,'width':width,'window_start':start,'settings_hashes':frozenset(settings)}
        self.handling=hours(config['handling_hours'],'handling_hours')
        self.transit=hours(config['supplier_to_hub_hours'],'supplier_to_hub_hours')
        setup=config['setup_seconds_per_plate']
        if type(setup) is not int or setup<0:raise ValueError('setup_seconds_per_plate must be a nonnegative integer')
        self.setup=Fraction(setup,3600)
        self._lock=threading.RLock();self._blocks={p:[] for p in self.printers};self._plans={};self._cancelled=set()
        for i,b in enumerate(config.get('existing_bookings',[])):
            pid=b['printer_id']
            if pid not in self.printers:raise ValueError('Existing booking references unknown printer')
            start=hours(b['start_hour'],'booking start');end=hours(b['finish_hour'],'booking finish')
            if end<=start or not b.get('source'):raise ValueError('Existing availability blocks need positive duration and source')
            if any(start<other[1] and other[0]<end for other in self._blocks[pid]):raise ValueError('Existing bookings overlap')
            self._blocks[pid].append((start,end,'external:'+str(i)))

    def _align(self,t,duration,p):
        if p['width']==24:return t
        if duration>p['width']:raise PrintUnavailable('Plate plus setup exceeds continuous operating window')
        day=math.floor((t-p['window_start'])/24)
        start=day*24+p['window_start']
        return t if t+duration<=start+p['width'] else start+24

    def preview(self,plates: list[Plate],day: int) -> PrintPlan:
        if type(day) is not int or day<0:raise ValueError('Quote day must be nonnegative integer')
        if not plates or len({p.plate_id for p in plates})!=len(plates):raise PrintUnavailable('Need distinct sliced plate IDs')
        with self._lock:
            blocks=deepcopy(self._blocks);slots=[]
            # Longest plates first, then deterministic earliest finish per printer.
            for plate in sorted(plates,key=lambda p:(-p.machine_seconds,p.plate_id)):
                if type(plate.machine_seconds) is not int or plate.machine_seconds<=0:raise PrintUnavailable('Invalid sliced duration')
                duration=Fraction(plate.machine_seconds,3600)+self.setup;candidates=[]
                for pid,p in self.printers.items():
                    if plate.profile_sha256!=p['profile_sha256'] or plate.settings_sha256 not in p['settings_hashes']:continue
                    if p.get('materials') is not None and plate.material not in p['materials']:continue
                    try:t=self._align(Fraction(day*24),duration,p)
                    except PrintUnavailable:continue
                    for start,end,_ in sorted(blocks[pid]):
                        if t+duration<=start:break
                        if t<end and start<t+duration:t=self._align(end,duration,p)
                    candidates.append((t+duration,t,pid))
                if not candidates:raise PrintUnavailable('No matching printer can fit this whole plate in a continuous window')
                finish,start,pid=min(candidates)
                slots.append(Slot(pid,plate,start,finish));blocks[pid].append((start,finish,'preview'))
            return PrintPlan(tuple(slots),max(s.finish for s in slots)+self.handling+self.transit)

    def reserve(self,plan: PrintPlan,reservation_id: str,day: int) -> None:
        """Recheck exact quoted slots and commit all of them, or none."""
        with self._lock:
            if reservation_id in self._cancelled:raise PrintUnavailable('Reservation already cancelled')
            if reservation_id in self._plans:
                if self._plans[reservation_id]!=plan:raise PrintUnavailable('Reservation ID reused for a different plan')
                return
            scratch=deepcopy(self._blocks)
            for s in plan.slots:
                if s.start<day*24:raise PrintUnavailable('Quoted production start has passed; request a new quote')
                if any(s.start<end and start<s.finish for start,end,_ in scratch[s.printer_id]):
                    raise PrintUnavailable('Quoted printer slot is no longer available; request a new quote')
                scratch[s.printer_id].append((s.start,s.finish,reservation_id))
            self._blocks=scratch;self._plans[reservation_id]=plan

    def cancel(self,reservation_id: str,day: int) -> None:
        """Explicitly abort active simulation work; retain elapsed occupancy only."""
        with self._lock:
            if reservation_id in self._cancelled:return
            if reservation_id not in self._plans:raise PrintUnavailable('Unknown printing reservation')
            now=Fraction(day*24)
            for pid,blocks in self._blocks.items():
                kept=[]
                for start,end,rid in blocks:
                    if rid!=reservation_id or end<=now:kept.append((start,end,rid))
                    elif start<now:kept.append((start,now,rid))
                self._blocks[pid]=kept
            self._cancelled.add(reservation_id)

    def snapshot(self):
        with self._lock:
            return {pid:[{'start_hour':float(a),'finish_hour':float(b),'reservation_id':rid}
                         for a,b,rid in sorted(blocks)] for pid,blocks in self._blocks.items()}

    def describe(self):
        return {pid:{'window_start_hour':float(p['window_start']),'operating_hours_per_day':float(p['width'])}
                for pid,p in self.printers.items()}
