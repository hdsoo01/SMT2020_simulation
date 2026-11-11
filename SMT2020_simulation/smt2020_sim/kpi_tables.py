"""Streaming operational CSVs. Raw DB history remains available for audit."""
from datetime import datetime, timedelta
from pathlib import Path
import csv
import json
import math
import os
import tempfile

from . import config
from .lot import BUCKETS
from .kpi_collector import WAIT_BUCKETS


def lot_summary_records(db, *, warmup, end, start=None, product=None,
                        lot_types=None, toolgroup=None, station=None, step=None,
                        use_compact=True):
    """New lots released in [max(start,warmup),end); no initial/carry-in lots.

    Status and waiting times are evaluated at end. WIP has no completion date.
    All wait bucket durations are intersected with the exact selected window.
    Group/station/step filters select lots visiting the scope in that window;
    the returned wait is their whole-lot wait, not only that scope's wait.
    """
    start=max(float(warmup),float(start) if start is not None else float(warmup))
    end=float(end)
    if not 0 <= start < end:
        raise ValueError('warm-up 이후의 유효한 측정기간이 필요합니다.')
    collection_row=db.execute("SELECT value FROM metadata WHERE key='collection'").fetchone()
    collection=json.loads(collection_row[0]) if collection_row else {}
    final_row=db.execute("SELECT value FROM metadata WHERE key='end_min'").fetchone()
    recorded_end=float(json.loads(final_row[0])) if final_row else None
    traces_cover=collection.get('full_visits',True) or (bool(lot_types) and set(lot_types).issubset(collection.get('trace_types',[])))
    if not traces_cover and (end!=recorded_end or any(x is not None for x in (toolgroup,station,step))):
        raise ValueError('경량 결과의 모든 lot CSV는 최종 시점의 제품/종류 조건으로 제공됩니다. 과거 시점·장비/step 방문 조건은 기록된 긴급 종류를 선택하거나 full 이력이 필요합니다.')
    conditions=['l.initial_wip=0','l.release_min>=?','l.release_min<?']
    params=[start,end]
    if product is not None:
        conditions.append('l.product=?');params.append(product)
    if lot_types:
        conditions.append('l.lot_type IN ('+','.join('?' for _ in lot_types)+')')
        params.extend(lot_types)
    extra=[];ep=[]
    for name,value in [('toolgroup',toolgroup),('station',station),('step_index',step)]:
        if value is not None:
            extra.append('v.'+name+'=?');ep.append(value)
    if extra:
        conditions.append('l.lot_id IN (SELECT v.lot_id FROM visits v WHERE '
                          'v.start_min<? AND v.end_min>? AND '+' AND '.join(extra)+')')
        params.extend([end,start]+ep)
    origin=config.SIM_START
    row=db.execute("SELECT value FROM metadata WHERE key='sim_start'").fetchone()
    if row:
        origin=datetime.fromisoformat(json.loads(row[0]))
    has_policy=db.execute("SELECT 1 FROM sqlite_master WHERE name='release_policy' AND type='table'").fetchone()
    policies={r[0]:r[1:] for r in db.execute('SELECT lot_id,due_base_min,due_z FROM release_policy')} if has_policy else {}
    def date(minutes):
        return (origin+timedelta(minutes=float(minutes))).isoformat(sep=' ',timespec='microseconds') if minutes is not None else None
    def record_for(lot_id,p,kind,priority,wafers,release,due,finish_min,status):
        observed=min(float(finish_min),end)
        complete=status=='completed' and finish_min<=end
        completion=float(finish_min) if complete else None
        record=dict(lot_id=lot_id,product=p,lot_type=kind,priority=priority,wafers=wafers,
            status='completed' if complete else 'wip',release_min=release,
            release_datetime=date(release),completion_min=completion,completion_datetime=date(completion),
            due_min=due,due_datetime=date(due),observed_until_min=observed,
            observed_until_datetime=date(observed),age_h=(observed-release)/60,
            cycle_time_h=(completion-release)/60 if complete else None,
            late_h=max(0,observed-due)/60,window_start_min=start,window_end_min=end,
            window_hours=(end-start)/60)
        record.update({b+'_h':0. for b in BUCKETS})
        record['due_base_min'],record['due_z']=policies.get(lot_id,(None,None))
        return record
    def finish(value):
        value['total_wait_h']=sum(value[b+'_h'] for b in WAIT_BUCKETS)
        value['total_observed_h']=sum(value[b+'_h'] for b in BUCKETS)
        return value
    end_row=db.execute("SELECT value FROM metadata WHERE key='end_min'").fetchone()
    if use_compact and end_row and end==float(json.loads(end_row[0])):
        # Every selected lot starts after start. At final observation end its saved
        # whole-lot buckets are exactly the requested clipped lifetime, including WIP.
        sql='SELECT l.lot_id,l.product,l.lot_type,l.priority,l.wafers,l.release_min,'\
            'l.due_min,l.end_min,l.status,l.buckets_json FROM lots l WHERE '\
            +' AND '.join(conditions)+' ORDER BY l.lot_id'
        for row in db.execute(sql,params):
            record=record_for(*row[:9])
            buckets=json.loads(row[9])
            for b in BUCKETS:
                record[b+'_h']=float(buckets.get(b,0))/60
            record=finish(record)
            if not math.isclose(record['age_h'],record['total_observed_h'],abs_tol=1e-6):
                raise ValueError('저장된 lot 시간 합과 관측 나이가 일치하지 않습니다: '+record['lot_id'])
            yield record
        return
    sql='''WITH selected AS (
        SELECT l.lot_id,l.product,l.lot_type,l.priority,l.wafers,l.release_min,
            l.due_min,l.end_min,l.status FROM lots l WHERE '''+' AND '.join(conditions)+'''
        ), totals AS (
            SELECT v.lot_id,s.bucket,SUM(MAX(0,MIN(s.end_min,?)-MAX(s.start_min,?))) AS minutes
            FROM selected c JOIN visits v ON v.lot_id=c.lot_id
            JOIN segments s USING(visit_id) WHERE s.start_min<? AND s.end_min>?
            GROUP BY v.lot_id,s.bucket)
        SELECT c.*,t.bucket,t.minutes FROM selected c LEFT JOIN totals t USING(lot_id)
        ORDER BY c.lot_id'''
    previous=None;record=None
    for row in db.execute(sql,params+[end,start,end,start]):
        lot_id,p,kind,priority,wafers,release,due,finish_min,status,bucket,minutes=row
        if previous!=lot_id:
            if record is not None:
                yield finish(record)
            record=record_for(lot_id,p,kind,priority,wafers,release,due,finish_min,status)
            previous=lot_id
        if bucket is not None:
            record[bucket+'_h']=float(minutes)/60
    if record is not None:
        yield finish(record)


def export_lot_summary(db, path, *, progress=None, **options):
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    rows=iter(lot_summary_records(db,**options))
    first=next(rows,None)
    fields=list(first) if first else ['lot_id','product','lot_type','priority','wafers','status',
        'release_min','release_datetime','completion_min','completion_datetime','due_min','due_datetime',
        'observed_until_min','observed_until_datetime','age_h','cycle_time_h','late_h',
        'window_start_min','window_end_min','window_hours']+[b+'_h' for b in BUCKETS]+['due_base_min','due_z','total_wait_h','total_observed_h']
    temporary=None
    count=0
    try:
        with tempfile.NamedTemporaryFile(mode='w',newline='',encoding='utf-8-sig',
                dir=path.parent,suffix='.tmp',delete=False) as stream:
            temporary=Path(stream.name)
            writer=csv.DictWriter(stream,fieldnames=fields)
            writer.writeheader()
            if first is not None:
                writer.writerow(first);count=1
            for row in rows:
                writer.writerow(row);count+=1
                if progress and count%20000==0:
                    progress(count)
        os.replace(temporary,path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    if progress:
        progress(count)
    origin_row=db.execute("SELECT value FROM metadata WHERE key='sim_start'").fetchone()
    definitions=dict(cohort='initial_wip=0; released in [max(start,warmup),end)',
        time_origin=json.loads(origin_row[0]) if origin_row else config.SIM_START.isoformat(),time_unit='minute',hours_per_year=365*24,
        completion='blank for WIP at selected observation end',
        total_wait_h='queue + wait_down + wait_batch + wait_gate + wait_dedicated + wait_reserved; clipped to selected window',
        age_h='release to min(actual completion, selected end)',
        late_h='positive overdue hours; completed lateness or current WIP overdue',
        scope='group/station/step filters select visiting lots; times cover each selected whole lot',
        aggregation='saved lot buckets at final observation end; clipped segments at historical endpoints',
        due_z='recorded parametric draw; blank for legacy/explicit data; never inferred in this CSV',
        interval_rule='[start,end); exact final partial interval denominator; no warm-up intervals')
    path.with_name(path.stem+'_definitions.json').write_text(json.dumps(definitions,indent=2,ensure_ascii=False),encoding='utf-8')
    return path
