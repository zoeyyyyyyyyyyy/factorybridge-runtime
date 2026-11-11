"""Sequential paid-run budget journal. No credentials or prompt contents stored.

Reservations use a conservative UTF-8 byte estimate plus framing allowance and
output cap; this is a client guard, not an authoritative provider billing cap.
Unknown usage blocks future requests until the owner reconciles the journal.
"""
from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
from uuid import uuid4


class SpendError(ValueError): pass


@contextmanager
def journal(path, limit):
    if not math.isfinite(limit) or limit<=0:raise SpendError('Spend limit must be positive')
    if os.name!='posix':raise SpendError('Budget journal requires POSIX file locking; run on macOS/Linux')
    import fcntl
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    with path.open('a+') as stream:
        os.chmod(path,0o600);fcntl.flock(stream,fcntl.LOCK_EX)
        try:
            stream.seek(0);raw=stream.read()
            data=json.loads(raw) if raw else {'limit_usd':limit,'entries':[]}
            if data['limit_usd']!=limit:raise SpendError('Cannot change an existing journal budget')
            yield data
            stream.seek(0);stream.truncate();json.dump(data,stream,allow_nan=False)
            stream.flush();os.fsync(stream.fileno())
        finally:fcntl.flock(stream,fcntl.LOCK_UN)


def reserve(path, limit, amount, model):
    if not math.isfinite(amount) or amount<0:raise SpendError('Invalid request reservation')
    with journal(path,limit) as data:
        if any(e['status']!='complete' for e in data['entries']):
            raise SpendError('Unreconciled request usage; no further paid request sent')
        spent=sum(e['budget_charge_usd'] for e in data['entries'])
        if spent+amount>limit:raise SpendError('Insufficient remaining budget for the next request')
        request_id=str(uuid4())
        data['entries'].append({'id':request_id,'model':model,'status':'reserved','reserved_usd':amount})
    return request_id


def settle(path, limit, request_id, charge):
    if not math.isfinite(charge) or charge<0:raise SpendError('Invalid accounted request cost')
    with journal(path,limit) as data:
        entry=next(e for e in data['entries'] if e['id']==request_id)
        if entry['status']!='reserved':raise SpendError('Request already settled')
        entry.update(status='complete',budget_charge_usd=charge)
        exceeded=sum(e.get('budget_charge_usd',e['reserved_usd']) for e in data['entries'])>limit
    if exceeded:raise SpendError('Reported use exceeded budget; stop and reconcile provider billing')
