"""P2 delivery scoring derived from HW7 section 6, independent of agent logic.

Only simulator truth determines the three outcome flags. Usage is metadata;
neither an agent's success claim nor the simulator's termination flag is a score.
Human refusal criteria are not inferred here. See docs/PERSON2_STATUS.md.
"""
from __future__ import annotations

import math

from schemas import Order


def _finite_nonnegative(value, name):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value < 0):
        raise ValueError(f'{name} must be finite and nonnegative')
    return value


def check(order: Order, truth: dict, tool_usage: dict, llm_usage: dict) -> dict:
    """Return delivery success; malformed truth raises rather than inventing data.

    Missing requested part entries count as zero good units. Extra good units
    and unrelated part entries cannot compensate for missing requested units.
    total_cost already includes cancellation fees; never add those fees again.
    """
    if not order.parts or len({p.part_id for p in order.parts}) != len(order.parts):
        raise ValueError('Order needs nonempty, unique parts')
    for part in order.parts:
        if type(part.qty) is not int or part.qty < 1:
            raise ValueError('Requested quantity must be a positive integer')
    if type(order.deadline_day) is not int or order.deadline_day < 0:
        raise ValueError('Deadline must be a nonnegative integer day')
    budget = _finite_nonnegative(order.budget_usd, 'budget_usd')
    if not isinstance(truth, dict) or not isinstance(truth.get('good_units'), dict):
        raise ValueError('Truth needs a good_units mapping')
    good = truth['good_units']
    for units in good.values():
        if type(units) is not int or units < 0:
            raise ValueError('Good units must be nonnegative integers')
    if 'final_delivery_day' not in truth or 'total_cost' not in truth:
        raise ValueError('Truth needs final_delivery_day and total_cost')
    delivered = truth['final_delivery_day']
    if delivered is not None and (type(delivered) is not int or delivered < 0):
        raise ValueError('Delivery must be a nonnegative integer day or None')
    cost = _finite_nonnegative(truth['total_cost'], 'total_cost')
    spec_ok = all(good.get(p.part_id, 0) >= p.qty for p in order.parts)
    on_time = delivered is not None and delivered <= order.deadline_day
    in_budget = cost <= budget
    result = {
        'success': spec_ok and on_time and in_budget,
        'spec_ok': spec_ok, 'on_time': on_time, 'in_budget': in_budget,
        'total_cost': cost,
        'slack_days': None if delivered is None else order.deadline_day - delivered,
        'tool_calls': tool_usage.get('tool_calls'),
        'replans': tool_usage.get('replans'),
    }
    # Explicit allowlist prevents usage dictionaries overwriting outcome flags.
    for key in ('llm_calls', 'input_tokens', 'output_tokens', 'usd',
                'accounting_complete', 'failed_calls'):
        result[key] = llm_usage.get(key)
    return result
