"""Bounded, read-only analyses of saved histories; never run the simulator."""
from array import array
from collections import Counter, defaultdict, OrderedDict
from contextlib import ExitStack, closing
import heapq
import json
import math
from pathlib import Path
import tempfile
import time

import numpy as np
import pandas as pd

from .kpi_collector import WAIT_BUCKETS
from .lot import BUCKETS


def visit_source(e, groups):
    """One sequential visits scan, or indexed lookup for selected lot types."""
    source, kinds = 'visits v', {}
    cond, params = e.conditions('v', available={'product','toolgroup','station','step_index'})
    kind_field = 'v.lot_id'
    if e.types:
        source = 'lots l CROSS JOIN visits v ON v.lot_id=l.lot_id'
        kind_field = 'l.lot_type'
        cond += ' AND l.lot_type IN (' + ','.join('?' for _ in e.types) + ')'
        params.extend(e.types)
    elif 'lot_type' in groups:
        kinds = dict(e.db.execute('SELECT lot_id,lot_type FROM lots'))
    fields = ','.join(kind_field if g=='lot_type' else 'v.'+g for g in groups)
    def group(row):
        return tuple(kinds[row[i]] if g=='lot_type' and not e.types else row[i]
                     for i,g in enumerate(groups))
    return source, fields, cond, params, group


def flow_totals(e,groups):
    """Linear bucket sums; only window-crossing visits need their segments."""
    e._check_traces()
    source,fields,cond,params,group_of=visit_source(e,groups)
    fields+=',v.visit_id,v.start_min,v.end_min,'+','.join('v.'+b+'_min' for b in BUCKETS)
    sql=f'SELECT {fields} FROM {source} WHERE v.start_min<? AND v.end_min>=?'+cond
    totals=defaultdict(lambda:np.zeros(len(BUCKETS)))
    bucket_index={b:i for i,b in enumerate(BUCKETS)}
    ncols=len(groups)
    for row in e.stream(sql,[e.end,e.start]+params):
        group=group_of(row);ident,a,b=row[ncols:ncols+3]
        if a>=e.start and b<=e.end:
            totals[group]+=row[ncols+3:]
        else:
            for bucket,left,right in e.db.execute('SELECT bucket,start_min,end_min FROM segments WHERE visit_id=?',(ident,)):
                duration=max(0,min(right,e.end)-max(left,e.start))
                if duration:totals[group][bucket_index[bucket]]+=duration
    frame=pd.DataFrame([dict(zip(groups,g),**dict(zip(BUCKETS,values/60))) for g,values in totals.items()],
                       columns=list(groups)+list(BUCKETS))
    frame['wait_lot_h']=frame[list(WAIT_BUCKETS)].sum(axis=1)
    frame['elapsed_lot_h']=frame[list(BUCKETS)].sum(axis=1)
    frame['mean_wip_lots']=frame.elapsed_lot_h/((e.end-e.start)/60)
    frame['mean_waiting_lots']=frame.wait_lot_h/((e.end-e.start)/60)
    return frame


def tails(e, groups, buckets, batch_only=False):
    """Disk-backed exact order statistics, with separate censored counts."""
    e._check_traces()
    source, fields, cond, params, group_of = visit_source(e, groups)
    if batch_only:
        cond += ' AND v.batch_id IS NOT NULL'
    expr = '+'.join('v.'+b+'_min' for b in buckets)
    sql = f'SELECT {fields},v.status,v.end_min,({expr})/60 FROM {source} '
    sql += "WHERE v.start_min>=? AND v.start_min<? AND v.status IN ('completed','censored')"+cond
    ncols = len(groups)
    counts, unfinished, files = Counter(), Counter(), {}
    started = time.monotonic()
    print('[대기 분포] 순차 조회 · 그룹별 숫자만 임시 보관 · 정확한 분위수',flush=True)
    with tempfile.TemporaryDirectory(prefix='smt_tails_') as directory:
        with ExitStack() as handles, closing(e.stream(sql,[e.start,e.end]+params)) as cursor:
            buffers, writers = defaultdict(lambda:array('d')), OrderedDict()
            handles.callback(lambda:[writer.close() for writer in writers.values()])
            def flush():
                for group, values in buffers.items():
                    if group not in writers:
                        if len(writers)>=512:_,old=writers.popitem(last=False);old.close()
                        if group not in files:files[group]=Path(directory)/(str(len(files))+'.bin')
                        writers[group]=files[group].open('ab',buffering=65536)
                    writers.move_to_end(group)
                    values.tofile(writers[group]); counts[group]+=len(values)
                buffers.clear()
            for i,row in enumerate(cursor,1):
                group=group_of(row)
                if row[ncols]=='completed' and row[ncols+1]<=e.end:
                    buffers[group].append(row[ncols+2])
                elif row[ncols+1]>=e.end:
                    unfinished[group]+=1
                if i%10000==0:flush()
            flush()
        print(f'[대기 분포] 완료 방문 {sum(counts.values()):,} · 미완료 {sum(unfinished.values()):,} '
              f'· {time.monotonic()-started:.1f}초',flush=True)
        rows=[]
        for group,path in files.items():
            n=counts[group];values=np.memmap(path,dtype=np.float64,mode='r+',shape=(n,))
            try:
                row=dict(zip(groups,group),n=n,mean=float(values.mean()),
                         minimum=float(values.min()),maximum=float(values.max()))
                positions={q:min(int(n*(q/100)),n-1) for q in (5,25,50,75,90,95)}
                values.partition(sorted(set(positions.values())))
                row.update({'p'+str(q):float(values[pos]) for q,pos in positions.items()})
                rows.append(row)
            finally:values._mmap.close()
        cols=list(groups)+['n','mean','minimum','maximum','p5','p25','p50','p75','p90','p95']
        distribution=pd.DataFrame(rows,columns=cols).sort_values('p95',ascending=False).reset_index(drop=True)
        cohorts=pd.DataFrame([dict(zip(groups,g),completed_n=counts[g],unfinished_n=unfinished[g])
                             for g in sorted(counts.keys()|unfinished.keys())],
                            columns=list(groups)+['completed_n','unfinished_n'])
    return distribution,cohorts


class TimeBins:
    """Accumulate interval lengths in O(1), including partial edge bins."""
    def __init__(self,e):
        self.e=e;self.count=int(np.ceil((e.end-e.start)/e.bin))
        self.data={}

    def add(self,key,a,b,weight=1.):
        e=self.e;a=max(a,e.start);b=min(b,e.end)
        if b<=a:return
        if key not in self.data:self.data[key]=(np.zeros(self.count),np.zeros(self.count+1))
        direct,difference=self.data[key]
        lo=min(int((a-e.start)//e.bin),self.count-1)
        hi=min(math.ceil((b-e.start)/e.bin)-1,self.count-1)
        if lo==hi:direct[lo]+=(b-a)*weight;return
        direct[lo]+=(e.start+(lo+1)*e.bin-a)*weight
        direct[hi]+=(b-(e.start+hi*e.bin))*weight
        difference[lo+1]+=weight;difference[hi]-=weight

    def values(self,key):
        direct,difference=self.data[key]
        return direct+np.cumsum(difference[:-1])*self.e.bin


def work_conditions(e):
    cond,params=e.conditions('w',available={'station','toolgroup'})
    # Narrow windows/groups can use the existing (station,start_min) index;
    # full windows stay a sequential work_id scan.
    full_end=e.snap.get('detail',{}).get('definitions',{}).get('end_min',e.end)
    if e.filters['station'] is None and (e.filters['toolgroup'] is not None or e.end-e.start<full_end*.1):
        selected=[st for st,info in e.snap.get('detail',{}).get('stations',{}).items()
                  if e.filters['toolgroup'] is None or info['toolgroup']==e.filters['toolgroup']]
        if selected:
            cond+=' AND w.station IN ('+','.join('?' for _ in selected)+')';params.extend(selected)
    return cond,params


def time_flow(e,groups):
    e._check_traces()
    key=('time-flow-stream-v2',tuple(groups))
    saved=e._load_cached(key)
    if saved is not None:return saved[0]['time_flow']
    wip,waiting=TimeBins(e),TimeBins(e)
    source,fields,cond,params,group_of=visit_source(e,groups)
    ncols=len(groups)
    if 'station' in groups or e.filters['station'] is not None:
        # Station attribution belongs to segments, not to a future assigned tool.
        kinds=dict(e.db.execute('SELECT lot_id,lot_type FROM lots'))
        cond,params=e.conditions('s',available={'station'})
        vc,vp=e.conditions('v',available={'product','toolgroup','step_index'})
        cond+=vc;params+=vp
        if e.types:
            cond+=' AND v.lot_id IN (SELECT lot_id FROM lots WHERE lot_type IN ('+','.join('?' for _ in e.types)+'))'
            params.extend(e.types)
        sql='SELECT '+','.join('v.lot_id' if g=='lot_type' else ('s.' if g=='station' else 'v.')+g for g in groups)
        sql+=' ,s.start_min,s.end_min,s.bucket FROM segments s JOIN visits v USING(visit_id) WHERE s.start_min<? AND s.end_min>?'+cond
        for row in e.stream(sql,[e.end,e.start]+params):
            group=tuple(kinds[row[i]] if g=='lot_type' else row[i] for i,g in enumerate(groups))
            a,b,bucket=row[ncols:];wip.add(group,a,b)
            if bucket in WAIT_BUCKETS:waiting.add(group,a,b)
    else:
        waits='+'.join('v.'+b+'_min' for b in WAIT_BUCKETS)
        sql=f'SELECT {fields},v.visit_id,v.start_min,v.end_min,({waits}) FROM {source} '
        sql+='WHERE v.start_min<? AND v.end_min>? AND v.end_min>v.start_min'+cond
        for row in e.stream(sql,[e.end,e.start]+params):
            group=group_of(row);ident,a,b,wait=row[ncols:]
            lo=int((a-e.start)//e.bin);hi=math.ceil((b-e.start)/e.bin)-1
            if a>=e.start and b<=e.end and lo==hi:
                wip.add(group,a,b)
                if group not in waiting.data:waiting.data[group]=(np.zeros(waiting.count),np.zeros(waiting.count+1))
                direct,_=waiting.data[group]
                direct[lo]+=wait
            else:
                # Only visits crossing a bin/window boundary need their state intervals.
                for bucket,left,right in e.db.execute(
                    'SELECT bucket,start_min,end_min FROM segments WHERE visit_id=?',(ident,)):
                    wip.add(group,left,right)
                    if bucket in WAIT_BUCKETS:waiting.add(group,left,right)
    rows=[]
    for group in sorted(wip.data,key=repr):
        minutes=wip.values(group)
        wait=waiting.values(group) if group in waiting.data else np.zeros(wip.count)
        for i in np.flatnonzero((minutes!=0)|(wait!=0)):
            a=e.start+int(i)*e.bin;b=min(a+e.bin,e.end);span=b-a
            rows.append(dict(zip(groups,group),bin=int(i),day=a/1440,period_start_min=a,
                period_end_min=b,period_hours=span/60,mean_wip_lots=minutes[i]/span,mean_waiting_lots=wait[i]/span))
    result=pd.DataFrame(rows,columns=list(groups)+['bin','day','period_start_min','period_end_min',
                                                'period_hours','mean_wip_lots','mean_waiting_lots'])
    e._save_cached(key,{'time_flow':result})
    return result


def work_metrics(e):
    """One chronological scan supplies E03/E04 and actual setup time in Q04."""
    from .kpi_explorer import UnavailableAnalysis
    if not e.collection.get('station_work',True):
        raise UnavailableAnalysis('작업 종류·시간대 분석은 full 장비 작업 이력이 필요합니다.')
    key=('work-metrics-stream-v1',)
    if key in e.cache:return e.cache[key]
    saved=e._load_cached(key)
    if saved is not None:e.cache[key]=saved[0];return saved[0]
    cond,params=work_conditions(e)
    # work_id is issued at start (collector.tool_work_start), giving chronological
    # per-station starts without an enormous sort or random station-index scan.
    sql='SELECT station,bucket,MAX(start_min,?),MIN(end_min,?) FROM station_work w '
    sql+='WHERE start_min<? AND end_min>?'+cond+' ORDER BY work_id'
    right,last_start={},{}
    totals=defaultdict(lambda:[0.,0,0.]);bins=TimeBins(e);examples=[]
    for station,bucket,a,b in e.stream(sql,[e.start,e.end,e.end,e.start]+params):
        if a<last_start.get(station,-np.inf):
            raise ValueError('station_work의 work_id 시작 순서가 일치하지 않습니다.')
        last_start[station]=a
        for group in ((station,bucket),(station,'ALL')):
            left=max(a,right.get(group,a))
            totals[group][0]+=max(0,b-left);totals[group][1]+=1;totals[group][2]+=b-a
            right[group]=max(b,right.get(group,b))
            if group[1]=='ALL':bins.add(station,left,b)
        if len(examples)<400:examples.append(dict(station=station,bucket=bucket,start_min=a,end_min=b))
    categories=pd.DataFrame([dict(station=k[0],bucket=k[1],union_h=v[0]/60,intervals=v[1],
        job_or_event_h=v[2]/60) for k,v in totals.items() if k[1]!='ALL'],
        columns=['station','bucket','union_h','intervals','job_or_event_h'])
    total=pd.DataFrame([dict(station=k[0],union_h=v[0]/60,intervals=v[1],job_or_event_h=v[2]/60)
                        for k,v in totals.items() if k[1]=='ALL'],
                       columns=['station','union_h','intervals','job_or_event_h'])
    rows=[]
    stations=e.snap.get('detail',{}).get('stations',{})
    selected={st for st,info in stations.items()
              if (e.filters['station'] is None or st==e.filters['station'])
              and (e.filters['toolgroup'] is None or info['toolgroup']==e.filters['toolgroup'])}|set(bins.data)
    for st in sorted(selected):
        values=bins.values(st) if st in bins.data else np.zeros(bins.count)
        for i,value in enumerate(values):
            a=e.start+i*e.bin;b=min(a+e.bin,e.end)
            rows.append(dict(station=st,day=a/1440,union_pct=100*value/(b-a),
                             period_start_min=a,period_end_min=b,period_hours=(b-a)/60))
    result={'categories':categories,'total':total,'bins':pd.DataFrame(rows,columns=['station','day',
        'union_pct','period_start_min','period_end_min','period_hours']),
        'gantt':pd.DataFrame(examples,columns=['station','bucket','start_min','end_min'])}
    e.cache[key]=result;e._save_cached(key,result)
    return result


def event_summary(e,prefix):
    """One scan caches both setup and dedication counts and bounded examples."""
    e._check_traces()
    key=('constraint-events-stream-v2',e.top)
    saved=e._load_cached(key)
    if saved is not None:
        tables=saved[0]
        family='setup' if prefix=='setup_' else 'dedication'
        return tables[family+'_counts'],tables[family+'_examples']
    kinds=dict(e.db.execute('SELECT lot_id,lot_type FROM lots'))
    cond,params=e.conditions('e',available={'product'})
    sql='SELECT event_id,lot_id,product,kind,time_min,attributes_json FROM events e '
    sql+="WHERE time_min>=? AND time_min<? AND (kind LIKE 'setup_%' OR kind LIKE 'dedication_target%')"+cond
    totals={f:defaultdict(lambda:[0,0.]) for f in ('setup','dedication')}
    examples={f:[] for f in totals}
    labels={'setup':['toolgroup','station','previous','setup'],
            'dedication':['product','lot_type','toolgroup','step_index','station']}
    for ident,lot,product,kind,t,attrs in e.stream(sql,[e.start,e.end]+params):
        if lot not in kinds or (e.types and kinds[lot] not in e.types):continue
        attrs=json.loads(attrs)
        if any(attrs.get(k)!=v for k,v in e.filters.items() if k in ('toolgroup','station','step_index') and v is not None):continue
        family='setup' if kind.startswith('setup_') else 'dedication'
        record=dict(event_id=ident,lot_id=lot,product=product,lot_type=kinds[lot],kind=kind,time_min=t)
        record.update(attrs)
        if len(examples[family])<e.top:examples[family].append(record)
        if family=='setup' and kind!='setup_change':continue
        group=tuple(record.get(k) for k in labels[family])
        totals[family][group][0]+=1
        totals[family][group][1]+=attrs.get('planned_duration_min',0) or 0
    tables={}
    for family in totals:
        count_name='transitions' if family=='setup' else 'events'
        tables[family+'_counts']=pd.DataFrame([dict(zip(labels[family],g),**{count_name:v[0]},planned_min=v[1])
            for g,v in totals[family].items()],columns=labels[family]+[count_name,'planned_min'])
        tables[family+'_examples']=pd.DataFrame(examples[family])
    e._save_cached(key,tables)
    family='setup' if prefix=='setup_' else 'dedication'
    return tables[family+'_counts'],tables[family+'_examples']


def quantity_split(e):
    """Integrate readiness using compressed threshold transitions, no range join."""
    from .kpi_explorer import UnavailableAnalysis
    e._check_traces()
    cond,params=e.conditions('q',available={'product','toolgroup','step_index'})
    profiles={};last={}
    sql='SELECT toolgroup,product,step_index,time_min,wafers<min_wafers FROM batch_queue_state q '
    sql+='WHERE time_min<=?'+cond+' ORDER BY state_id'
    for tg,product,step,t,short in e.stream(sql,[e.end]+params):
        group=(tg,product,step)
        previous=last.get(group)
        if previous and t<previous[0]:raise ValueError('배치 상태의 state_id 시간 순서가 일치하지 않습니다.')
        last[group]=(t,short)
        if t<e.start:continue
        if group not in profiles:
            a=e.start if previous else t
            initial=previous[1] if previous else short
            profiles[group]=(array('d',[a]),array('d',[0.]),array('d',[initial]))
        times,prefix,flags=profiles[group]
        if short!=flags[-1]:
            prefix.append(prefix[-1]+(t-times[-1])*flags[-1]);times.append(t);flags.append(short)
    for group,(t,short) in last.items():
        if group not in profiles and t<=e.start:
            profiles[group]=(array('d',[e.start]),array('d',[0.]),array('d',[short]))
    profiles={g:tuple(np.asarray(x) for x in xs) for g,xs in profiles.items()}
    def integral(profile,values):
        times,prefix,flags=profile;idx=np.searchsorted(times,values,side='right')-1
        if np.any(idx<0):raise UnavailableAnalysis('배치 수량 상태 이력이 대기 시작을 덮지 않습니다.')
        return prefix[idx]+(values-times[idx])*flags[idx]
    source,fields,cond,params,group_of=visit_source(e,('toolgroup','product','step_index','lot_type'))
    sql=f'SELECT {fields},MAX(s.start_min,?),MIN(s.end_min,?) FROM {source} '
    sql+='CROSS JOIN segments s ON s.visit_id=v.visit_id WHERE v.wait_batch_min>0 '
    sql+="AND s.bucket='wait_batch' AND s.start_min<? AND s.end_min>?"+cond
    totals=defaultdict(lambda:np.zeros(2));buffers=defaultdict(list)
    def flush():
        for group,intervals in buffers.items():
            profile=profiles.get(group[:3])
            if profile is None:raise UnavailableAnalysis('대기 구간에 대응하는 배치 수량 상태 이력이 없습니다.')
            spans=np.asarray(intervals);short=integral(profile,spans[:,1])-integral(profile,spans[:,0])
            duration=spans[:,1]-spans[:,0]
            if np.any(short<-1e-6) or np.any(short-duration>1e-6):raise ValueError('수량 대기 분리의 시간 합이 일치하지 않습니다.')
            totals[group]+=np.array([short.sum(),(duration-short).sum()])
        buffers.clear()
    for i,row in enumerate(e.stream(sql,[e.start,e.end,e.end,e.start]+params),1):
        buffers[group_of(row)].append(tuple(row[4:]))
        if i%10000==0:flush()
    flush()
    return pd.DataFrame([dict(toolgroup=g[0],product=g[1],step_index=g[2],lot_type=g[3],
        quantity_short_lot_h=v[0]/60,quantity_ready_lot_h=v[1]/60) for g,v in totals.items()],
        columns=['product','step_index','toolgroup','lot_type','quantity_short_lot_h','quantity_ready_lot_h'])


def allocation(e):
    """Sweep ordered starts and a small per-tool end heap; avoid UNION sort."""
    cond,params=work_conditions(e)
    sql='''SELECT w.station,w.work_id,w.bucket,MAX(w.start_min,?),MIN(w.end_min,?),
        (SELECT json_group_array(json_object('product',m.product,'lot_type',m.lot_type,
        'wafers',m.wafers,'step_index',v.step_index)) FROM work_members m
        LEFT JOIN visits v USING(visit_id) WHERE m.work_id=w.work_id)
        FROM station_work w WHERE w.start_min<? AND w.end_min>?'''+cond+' ORDER BY w.work_id'
    states={};totals=defaultdict(float);unknown=0.
    def advance(st,state,t):
        nonlocal unknown
        active,ends,last=state
        def emit(until):
            nonlocal unknown,last
            if active and until>last:
                share=(until-last)/len(active)
                for bucket,members in active.values():
                    if not members:
                        unknown+=share;totals[(st,'UNLINKED','UNLINKED',None,bucket)]+=share
                    else:
                        wafers=sum(m['wafers'] for m in members)
                        if wafers<=0:raise ValueError('배치 구성원 wafer 합이 양수가 아닙니다.')
                        for m in members:
                            totals[(st,m['product'],m['lot_type'],m['step_index'],bucket)]+=share*m['wafers']/wafers
            last=until
        while ends and ends[0][0]<=t:
            end,ident=heapq.heappop(ends);emit(end);active.pop(ident,None)
        emit(t);state[2]=last
    for st,ident,bucket,a,b,members in e.stream(sql,[e.start,e.end,e.end,e.start]+params):
        state=states.setdefault(st,[{},[],a])
        if a<state[2]:raise ValueError('장비 작업의 시작 순서가 일치하지 않습니다.')
        advance(st,state,a)
        if b>a:
            state[0][ident]=(bucket,json.loads(members) if members else [])
            heapq.heappush(state[1],(b,ident))
    for st,state in states.items():advance(st,state,e.end)
    frame=pd.DataFrame([dict(station=g[0],product=g[1],lot_type=g[2],step_index=g[3],bucket=g[4],allocated_h=v/60)
                        for g,v in totals.items()],columns=['station','product','lot_type','step_index','bucket','allocated_h'])
    for col in ('product','step_index'):
        if e.filters[col] is not None:frame=frame[frame[col]==e.filters[col]]
    if e.types:frame=frame[frame.lot_type.isin(e.types)]
    return frame,unknown/60
