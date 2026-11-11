"""Shared prompt skeleton. P1 must import this exact constant for parity."""
SYSTEM_PROMPT = '''Fulfil this order by its deadline within its budget and to its specifications.
Use only the provided tools and actual tool results. All suppliers deliver parts
to the hub; assembly takes assembly_days after the last good part arrives. Award
one hub-to-customer shipping package too; pickup shifts to readiness if necessary.
Payment occurs at award; cancellation costs are defined by the shared contract.
Refused reservations are not production. Respond to delays and inspections.
A part award includes transport ONLY to the hub, never final customer shipping.
Customer shipping requires its OWN discovered shipping supplier, quote and confirmed
reservation. A null get_inspection result means no evidence is available yet;
it NEVER means passed inspection. Never claim delivery from a plan or message.
Unknown facts remain unknown.
Keep reasoning concise. Tool calls and model usage are limited and recorded.
The independent checker, not your answer, decides success.'''


def public_order(order) -> dict:
    """Customer requirements only; future scripted failures belong to World.

    P1 must use this helper too. The exact Order dataclass remains unchanged.
    """
    from dataclasses import asdict
    result=asdict(order)
    result.pop('injected_events',None)
    return result
