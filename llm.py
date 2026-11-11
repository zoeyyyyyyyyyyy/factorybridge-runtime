"""Shared native tool-calling transport and explicit usage accounting for both agents.

No request on import. MODEL must be set before invocation; no silent fallback.
Only local endpoints are enabled by default. Paid/remote endpoints require an
explicit FACTORYBRIDGE_ALLOW_REMOTE=1 opt-in outside this task.
"""
from __future__ import annotations
import ipaddress
import json
import math
import os
from pathlib import Path
import threading
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

MODEL = os.environ.get('MODEL', '')  # import-safe; resolved strictly at invocation
_LOCK = threading.Lock()
_STATE: dict = {}

class LLMError(RuntimeError): pass
class LLMBudgetExceeded(LLMError): pass
class LLMSpendBudgetExceeded(LLMBudgetExceeded): pass

def reset_usage() -> None:
    with _LOCK:
        _STATE.clear()
        _STATE.update(llm_calls=0,input_tokens=0,output_tokens=0,usd=0.0,
                      failed_calls=0,accounting_complete=True,model=None)

def usage() -> dict:
    with _LOCK:return dict(_STATE)

reset_usage()

def _endpoint() -> str:
    base=os.environ.get('LLM_BASE_URL','http://172.17.0.1:8766/v1').rstrip('/')
    parsed=urlsplit(base)
    if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password:
        raise LLMError('LLM_BASE_URL must be an HTTP(S) endpoint without embedded credentials')
    host=parsed.hostname
    try:
        address=ipaddress.ip_address(host)
        local=address.is_loopback or address.is_private
    except ValueError:local=host in ('localhost','host.docker.internal')
    if not local and os.environ.get('FACTORYBRIDGE_ALLOW_REMOTE')!='1':
        raise LLMError('Remote inference is disabled; no paid API request was sent')
    return base+'/chat/completions'

def _token() -> str:
    if os.environ.get('LLM_API_KEY'):return os.environ['LLM_API_KEY']
    if os.environ.get('LLM_API_KEY_FILE'):return Path(os.environ['LLM_API_KEY_FILE']).read_text().strip()
    return ''

def call_llm(messages: list[dict], tools: list[dict]) -> dict:
    """Return one OpenAI-compatible assistant message with native tool_calls.

    Provider usage is mandatory (never guessed). Prices must be explicitly
    configured per million tokens, including zero for the existing local model.
    An unknown cost is None, not a fabricated $0. Failed requests are counted.
    Both conditions use identical MODEL, temperature=0 and configured budgets.
    """
    model=os.environ.get('MODEL')
    if not model:raise LLMError('Set MODEL explicitly for both benchmark conditions')
    url=_endpoint()
    rates=[os.environ.get(k) for k in ('LLM_INPUT_USD_PER_MILLION','LLM_OUTPUT_USD_PER_MILLION')]
    a=b=None
    if all(v is not None for v in rates):
        try:a,b=map(float,rates)
        except ValueError:raise LLMError('Explicit token prices must be numeric') from None
        if not all(math.isfinite(v) and v>=0 for v in (a,b)):raise LLMError('Explicit token prices must be finite and nonnegative')
    token=_token()
    max_output=int(os.environ.get('LLM_MAX_OUTPUT_TOKENS','2048'))
    max_calls=int(os.environ.get('LLM_MAX_CALLS','40'))
    token_cap=int(os.environ.get('LLM_MAX_TOTAL_TOKENS','100000'))
    effort=os.environ.get('LLM_REASONING_EFFORT')
    token_parameter=os.environ.get('LLM_TOKEN_PARAMETER','max_tokens')
    if token_parameter not in ('max_tokens','max_completion_tokens'):
        raise LLMError('Unknown completion-token parameter')
    if effort not in (None,'none'):
        raise LLMError('This temperature-zero benchmark transport supports explicit reasoning none only')
    body={'model':model,'temperature':0,'messages':messages,token_parameter:max_output,'stream':False}
    if effort is not None:body['reasoning_effort']=effort
    if tools:body.update(tools=tools,tool_choice='auto')
    encoded=json.dumps(body,allow_nan=False).encode()
    ledger=os.environ.get('LLM_SPEND_LEDGER_FILE');reservation_id=None
    if ledger:
        from bench.spend import reserve, SpendError
        if a is None or b is None:raise LLMError('Budget guard requires explicit token rates')
        limit=float(os.environ['LLM_TOTAL_USD_BUDGET'])
        # Byte-count allowance is deliberately much larger than usual token use.
        # 25% input margin covers the published 30m cache-write premium.
        input_allowance=len(encoded)+4096+256*(len(messages)+len(tools or []))
        reservation_usd=(input_allowance*a*1.25+max_output*b)/1_000_000
    with _LOCK:
        if not _STATE['accounting_complete']:raise LLMBudgetExceeded('Unknown previous usage; reset before a new run')
        if _STATE['model'] not in (None,model):raise LLMError('Cannot change MODEL within a run')
        remaining=token_cap-_STATE['input_tokens']-_STATE['output_tokens']
        if _STATE['llm_calls']>=max_calls or remaining<=max_output:
            raise LLMBudgetExceeded('Shared LLM call/token budget exhausted')
        if ledger:
            try:reservation_id=reserve(ledger,limit,reservation_usd,model)
            except (SpendError,OSError,ValueError) as exc:raise LLMSpendBudgetExceeded(str(exc)) from None
        _STATE['model']=model;_STATE['llm_calls']+=1
    headers={'Content-Type':'application/json'}
    if token:headers['Authorization']='Bearer '+token
    try:
        request=Request(url,data=encoded,headers=headers,method='POST')
        with urlopen(request,timeout=float(os.environ.get('LLM_TIMEOUT_S','600'))) as response:
            data=json.load(response)
        counted=data['usage'];inp=counted['prompt_tokens'];out=counted['completion_tokens']
        if type(inp) is not int or type(out) is not int or inp<0 or out<0:raise ValueError('Invalid provider token counts')
        message=data['choices'][0]['message']
        if message.get('role')!='assistant':raise ValueError('Provider returned no assistant message')
    except Exception as exc:
        with _LOCK:
            _STATE['failed_calls']+=1;_STATE['accounting_complete']=False;_STATE['usd']=None
        # Never echo provider bodies, request headers or keys into traces.
        raise LLMError('Inference failed or usage missing ('+type(exc).__name__+'); request counted, token/cost totals incomplete') from None
    cost=None if a is None or b is None else (inp*a+out*b)/1_000_000
    with _LOCK:
        _STATE['input_tokens']+=inp;_STATE['output_tokens']+=out
        _STATE['usd']=None if cost is None or _STATE['usd'] is None else _STATE['usd']+cost
        over=_STATE['input_tokens']+_STATE['output_tokens']>token_cap
    if reservation_id is not None:
        from bench.spend import settle, SpendError
        try:settle(ledger,limit,reservation_id,(inp*a*1.25+out*b)/1_000_000)
        except (SpendError,OSError,ValueError) as exc:raise LLMSpendBudgetExceeded(str(exc)) from None
    if over:raise LLMBudgetExceeded('Provider-reported tokens exceeded the per-run cap; usage retained')
    return message
