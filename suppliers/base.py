"""Catalog parsing for explicitly simulated suppliers; no market observations."""
from __future__ import annotations
from copy import deepcopy
import math


def number(value, name, *, minimum=0, maximum=None):
    if (isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value)
            or value<minimum or (maximum is not None and value>maximum)):
        raise ValueError(f'Invalid finite {name}')
    return value


def validate_catalog(raw: dict) -> dict:
    if not isinstance(raw,dict):raise ValueError('Catalog must be an object')
    if raw.get('price_origin') != 'simulation_assumptions':
        raise ValueError('Catalog must explicitly label price_origin=simulation_assumptions; not observed market data')
    if raw.get('review_status')=='reference_only_incomplete':
        raise ValueError('Reference-only catalog: fill and review manufacturing limits before running')
    suppliers=raw.get('suppliers')
    if not isinstance(suppliers,list) or not suppliers:
        raise ValueError('Catalog needs a nonempty suppliers list')
    ids=set()
    for entry in suppliers:
        if not isinstance(entry,dict):raise ValueError('Supplier entry must be an object')
        sid=entry.get('supplier_id')
        if not isinstance(sid,str) or not sid or sid in ids:raise ValueError('Supplier IDs must be unique strings')
        ids.add(sid)
        if entry.get('kind') not in ('print','pcb','purchased','shipping'):raise ValueError('Unknown supplier kind')
        for key in ('advertised_on_time_rate','actual_on_time_rate','defect_rate'):
            number(entry[key],key,maximum=1)
        if entry['kind']=='print':
            if entry.get('capacity_per_day') is not None or entry.get('lead_days') is not None:
                raise ValueError('Printing uses print_capacity calendars; remove parts/day and fixed lead_days')
            from suppliers.print_calendar import PrintCalendar
            PrintCalendar(entry.get('print_capacity'))
        else:
            if type(entry['lead_days']) is not int or entry['lead_days']<1:raise ValueError('lead_days must be positive integer')
            cap=entry['capacity_per_day']
            if cap is not None and (type(cap) is not int or cap<1):raise ValueError('capacity_per_day must be positive integer or null')
        if entry.get('adapter')=='factoryos_pla':
            if entry['kind']!='print':raise ValueError('FactoryOS adapter needs print kind')
            continue
        if entry.get('adapter','rule_based')!='rule_based':raise ValueError('Unknown supplier adapter')
        price=entry['pricing']
        mode=price.get('mode','per_cm3')
        if entry['kind']=='print' and mode not in ('per_cm3','time','per_gram_inclusive'):
            raise ValueError('Unknown explicit print pricing mode')
        print_keys={'per_cm3':('setup_usd','per_cm3_usd'),'time':('setup_usd','material_usd_per_g','machine_usd_per_hour'),
                    'per_gram_inclusive':('setup_usd','per_gram_usd')}
        keys={'print':print_keys.get(mode,()),'pcb':('setup_usd',),
              'purchased':('handling_usd',),'shipping':('flat_usd',)}[entry['kind']]
        for key in keys:number(price[key],key)
        if entry['kind']=='pcb':
            if not any(key in price for key in ('per_board_usd','per_cm2_layer_usd')):raise ValueError('PCB unit price required')
            for key in ('per_board_usd','per_cm2_layer_usd'):
                if key in price:number(price[key],key)
        if entry['kind']=='purchased':
            if not isinstance(price.get('sku_prices_usd'),dict):raise ValueError('Purchased supplier needs SKU prices')
            for value in price['sku_prices_usd'].values():number(value,'SKU price')
        if entry['kind'] in ('print','pcb'):
            limits=entry['limits']
            if not isinstance(limits,dict):raise ValueError('Manufacturing limits must be configured')
            number(limits['min_tolerance_mm'],'min tolerance',minimum=0)
            if entry['kind']=='print':
                number(limits['max_dim_mm'],'max dimension',minimum=.001)
                if not entry.get('materials'):raise ValueError('Print supplier needs materials')
            else:
                if not limits.get('supported_layers') or any(type(x) is not int or x<1 for x in limits['supported_layers']):raise ValueError('PCB supported_layers required')
                dims=limits['max_size_mm']
                if len(dims)!=2:raise ValueError('PCB maximum size must have two dimensions')
                for value in dims:number(value,'PCB maximum size',minimum=.001)
        number(entry.get('price_jitter_fraction',.1),'price jitter',maximum=.1)
        for qty,factor in entry.get('discount_tiers',[]):
            if type(qty) is not int or qty<1:raise ValueError('Discount quantity must be positive integer')
            number(factor,'discount factor',minimum=0,maximum=1)
    return deepcopy(raw)
