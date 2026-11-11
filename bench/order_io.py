"""Strict shared Order parser. Does not create scenarios or expected answers."""
from dataclasses import fields
import math
from schemas import Order,Part


def parse_order(raw: dict) -> Order:
    if not isinstance(raw,dict):raise ValueError('Case must be a shared Order JSON object')
    unknown=set(raw)-{f.name for f in fields(Order)}
    if unknown:raise ValueError('Unknown Order fields; keep human answer keys separate from agent inputs')
    try:
        values=dict(raw);values['parts']=[Part(**p) for p in raw['parts']]
        order=Order(**values)
    except (TypeError,KeyError):raise ValueError('Missing/invalid shared Order or Part fields') from None
    if not isinstance(order.order_id,str) or not order.order_id:raise ValueError('order_id required')
    if type(order.deadline_day) is not int or order.deadline_day<0:raise ValueError('deadline_day must be nonnegative integer')
    if isinstance(order.budget_usd,bool) or not isinstance(order.budget_usd,(int,float)) or not math.isfinite(order.budget_usd) or order.budget_usd<0:raise ValueError('budget_usd must be finite and nonnegative')
    if type(order.assembly_days) is not int or order.assembly_days<0:raise ValueError('assembly_days must be nonnegative integer')
    if not order.parts or len({p.part_id for p in order.parts})!=len(order.parts):raise ValueError('Parts must be nonempty with unique IDs')
    for p in order.parts:
        if not isinstance(p.part_id,str) or not p.part_id or p.kind not in ('print','pcb','purchased') or type(p.qty) is not int or p.qty<1:raise ValueError('Invalid part ID/kind/quantity')
    if not isinstance(order.injected_events,list):raise ValueError('injected_events must be a list')
    return order
