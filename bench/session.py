"""Reusable run loop. Scoring MUST be supplied by the independent checker owner.

Does not open cases, catalogs, answer keys or checker source. No self-scoring
fallback. Without a checker, returns scored=False and no success field.
"""
from __future__ import annotations
import os
import time
from typing import Callable
from copy import deepcopy
from agents.common import public_order
import llm
from schemas import Order
from tools_api import Agent,Tools,World

Checker = Callable[[Order,dict,dict,dict],dict]


def run_session(order: Order,agent: Agent,world: World,tools: Tools,*,
                check: Checker | None = None,checker_identity: str | None = None,observer: Callable | None = None) -> dict:
    if world.day!=0 or tools.usage()['attempted_calls']!=0:
        raise ValueError('Each run needs a new World, Marketplace and Agent')
    if check is not None and not checker_identity:
        raise ValueError('Independent checker identity is required for scored runs')
    llm.reset_usage();started=time.perf_counter();error=None;observer_errors=0
    def emit(phase):
        nonlocal observer_errors
        if observer is not None:
            try:observer({'condition':agent.name,'order':public_order(order),'phase':phase,
                'truth':world.ground_truth(),'tool_usage':tools.usage(),'llm_usage':llm.usage()})
            except Exception:observer_errors+=1
    try:
        emit('Agent processing the order; waiting for recorded tool activity')
        agent.start(order,tools)
        emit('Initial planning turn finished')
        while world.day<order.deadline_day+5 and not world.ground_truth()['finished']:
            updates,inspections=world.advance_day()
            emit('Supplier events recorded; agent may respond')
            if updates or inspections:agent.on_events(world.day,updates,inspections,tools)
    except Exception as exc:
        error={'stage':'execution','type':type(exc).__name__,'message':str(exc)}
    truth=world.ground_truth();tool_usage=tools.usage();model_usage=llm.usage()
    result={'order':public_order(order),'observer_errors':observer_errors+getattr(tools,'observer_errors',0),'condition':agent.name,'scored':False,'checker_identity':checker_identity,
            'world_metadata':{'seed':getattr(world,'seed',None),'unreliability':getattr(world,'unreliability',None),'catalog_sha256':getattr(world,'catalog_sha256',None)},
            'execution_failed':error is not None,'error':error,'truth':truth,
            'tool_usage':tool_usage,'llm_usage':model_usage,
            'model':model_usage['model'] or (os.environ.get('MODEL') if agent.name!='greedy' else None),
            'llm_invoked':model_usage['llm_calls']>0,'wall_s':time.perf_counter()-started,
            'trace':tools.trace(),'agent_messages':getattr(agent,'messages',[])}
    if check is not None:
        try:
            verdict=check(order,truth,tool_usage,model_usage)
            if not isinstance(verdict,dict) or any(type(verdict.get(k)) is not bool for k in ('success','spec_ok','on_time','in_budget')):
                raise ValueError('Checker must return explicit success/spec_ok/on_time/in_budget booleans')
            result['independent_verdict']=verdict
            result['scored']=True
            result.update({k:verdict[k] for k in ('success','spec_ok','on_time','in_budget')})
            if error is not None:result['success']=False
        except Exception as exc:
            result['checker_error']={'type':type(exc).__name__,'message':str(exc)}
    return result
