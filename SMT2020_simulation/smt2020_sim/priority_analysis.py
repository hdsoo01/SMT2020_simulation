"""Small lot-table analyses; no scan of full visit/segment histories.

Removing a waiting bucket or rescoring z=1 holds the observed trajectory fixed.
These diagnostics are not causal policy simulations: due dates also change CR.
"""
from pathlib import Path
import json
import math

import numpy as np
import pandas as pd

from .lot import BUCKETS
from .kpi_collector import WAIT_BUCKETS


def due_offsets(xlsx):
    from openpyxl import load_workbook
    from .data_parser import _parse_release
    wb=load_workbook(xlsx,read_only=True,data_only=True)
    try:
        streams=_parse_release(wb)
    finally:
        wb.close()
    out={}
    for s in streams:
        kind='superhot' if s.is_superhot else 'hot' if s.priority>=20 else 'regular'
        key=(s.product,kind)
        if key in out and out[key]!=s.due_date_offset:
            raise ValueError('같은 제품·종류의 서로 다른 납기 기준은 lot별 저장값이 필요합니다.')
        out[key]=s.due_date_offset
    return out


def lot_frame(db, *, warmup, end, offsets=None):
    frame=pd.read_sql_query('SELECT * FROM lots WHERE initial_wip=0 AND release_min>=? AND release_min<?',
                            db,params=[warmup,end])
    buckets=frame.buckets_json.map(json.loads)
    for b in BUCKETS:
        frame[b+'_h']=buckets.map(lambda x:x.get(b,0)/60)
    frame['observed_end_min']=np.minimum(frame.end_min,end)
    frame['completed']=(frame.status=='completed')&(frame.end_min<=end)
    frame['ct_h']=(frame.observed_end_min-frame.release_min)/60
    frame['due_h']=(frame.due_min-frame.release_min)/60
    frame['late_h']=(frame.observed_end_min-frame.due_min).clip(lower=0)/60
    frame['on_time']=frame.completed&(frame.end_min<=frame.due_min)
    frame['due_base_min']=np.nan
    frame['due_z']=np.nan
    tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'release_policy' in tables:
        policy=pd.read_sql_query('SELECT lot_id,due_base_min,due_z FROM release_policy',db)
        frame=frame.drop(columns=['due_base_min','due_z']).merge(policy,on='lot_id',how='left')
    if offsets:
        missing=frame.due_base_min.isna()
        frame.loc[missing,'due_base_min']=[offsets.get((r.product,r.lot_type),np.nan)
                                         for r in frame.loc[missing].itertuples()]
        infer=frame.due_z.isna()&frame.due_base_min.notna()
        frame.loc[infer,'due_z']=(frame.due_min-frame.release_min)/frame.due_base_min
    frame['due_base_min']=pd.to_numeric(frame.due_base_min,errors='coerce')
    frame['due_z']=pd.to_numeric(frame.due_z,errors='coerce')
    frame['base_due_h']=frame.due_base_min/60
    frame['total_wait_h']=frame[[b+'_h' for b in WAIT_BUCKETS]].sum(axis=1)
    frame['nonwaiting_h']=frame.ct_h-frame.total_wait_h
    return frame


def wilson(k,n):
    if not n: return np.nan,np.nan
    z=1.959963984540054;p=k/n;den=1+z*z/n
    center=(p+z*z/(2*n))/den
    half=z*math.sqrt(p*(1-p)/n+z*z/(4*n*n))/den
    return 100*(center-half),100*(center+half)


def service_tables(frame):
    done=frame[frame.completed].copy()
    rows=[];causes=[]
    for (product,kind),g in done.groupby(['product','lot_type']):
        n=len(g);k=int(g.on_time.sum());lo,hi=wilson(k,n)
        late=g[~g.on_time]
        row=dict(product=product,lot_type=kind,n=n,on_time_pct=100*k/n,
            on_time_low95_pct=lo,on_time_high95_pct=hi,priority=g.priority.iloc[0],
            due_mean_h=g.due_h.mean(),due_p10_h=g.due_h.quantile(.1),
            ct_mean_h=g.ct_h.mean(),ct_p95_h=g.ct_h.quantile(.95),
            z_mean=g.due_z.mean(),z_low_pct=100*(g.due_z<1).mean() if g.due_z.notna().any() else np.nan,
            mean_queue_h=g.queue_h.mean(),mean_batch_wait_h=g.wait_batch_h.mean(),
            mean_total_wait_h=g.total_wait_h.mean(),mean_reworks=g.rework_count.mean(),
            mean_positive_late_h=late.late_h.mean(),
            z1_rescore_on_time_pct=100*(g.ct_h<=g.base_due_h).mean() if g.base_due_h.notna().all() else np.nan,
            uniform_z_rescore_on_time_pct=100*np.clip((1.2-g.ct_h/g.base_due_h)/.4,0,1).mean() if g.base_due_h.notna().all() else np.nan,
            no_batch_rescore_on_time_pct=100*(g.ct_h-g.wait_batch_h<=g.due_h).mean(),
            nonwaiting_exceeds_due_n=int((g.nonwaiting_h>g.due_h).sum()))
        rows.append(row)
        for b in WAIT_BUCKETS:
            causes.append(dict(product=product,lot_type=kind,bucket=b,late_n=len(late),
                mean_h=g[b+'_h'].mean(),mean_late_lot_h=late[b+'_h'].mean(),
                late_rescued_if_removed_n=int((late.ct_h-late[b+'_h']<=late.due_h).sum())))
    zrows=[]
    known=done[done.due_z.notna()].copy()
    # explicit 시트의 납기는 초 단위로 반올림돼 역산 z가 양 끝을 최대 2e-7 넘는다(0.01분 미만).
    # 경계 여유가 1e-9이면 그런 lot이 구간 표에서 조용히 빠진다.
    tol=1e-6
    known['z_band']=pd.cut(known.due_z,[.8-tol,.9,1,1.1,1.2+tol],
                           labels=['0.8–0.9','0.9–1.0','1.0–1.1','1.1–1.2'],right=False)
    for (p,t,b),g in known.groupby(['product','lot_type','z_band'],observed=True):
        zrows.append(dict(product=p,lot_type=t,z_band=str(b),n=len(g),
            on_time_pct=100*g.on_time.mean(),ct_mean_h=g.ct_h.mean(),due_mean_h=g.due_h.mean()))
    wip=frame[~frame.completed].copy()
    # All releases whose deadlines have passed: completed and unfinished overdue lots.
    mature=frame[frame.due_min<=frame.window_end_min] if 'window_end_min' in frame else frame.iloc[:0]
    return dict(priority_service=pd.DataFrame(rows),waiting_causes=pd.DataFrame(causes),
        release_due_distribution=frame.groupby(['product','lot_type']).agg(
            released_n=('lot_id','size'),z_mean=('due_z','mean'),z_std=('due_z','std'),
            due_mean_h=('due_h','mean'),completed_n=('completed','sum')).reset_index(),
        due_z_bands=pd.DataFrame(zrows),
        urgent_late_lots=done[(done.lot_type!='regular')&(~done.on_time)]
            .drop(columns=['buckets_json']).sort_values('late_h',ascending=False),
        wip_due= wip[['lot_id','product','lot_type','priority','due_min','ct_h','late_h']],
        mature_release_service=mature.groupby(['product','lot_type']).agg(
            n=('lot_id','size'),on_time_pct=('on_time',lambda x:100*x.mean())).reset_index())


def analyze_database(db, *, warmup,end,offsets=None):
    frame=lot_frame(db,warmup=warmup,end=end,offsets=offsets)
    frame['window_end_min']=end
    return service_tables(frame)


def for_snapshot(db,snapshot):
    """저장된 z가 없는 lot은 워크북 `Lotrelease`의 기준 여유(FF·RPT)로 z를 역산한다.

    이전 parametric 결과: 생성 시 뽑은 z를 복원한다.
    explicit: 시트의 DUE가 이미 z·FF·RPT(논문 식 1)이므로 z = 실제 여유 / 기준 여유.
    진단용 값이며 모델은 읽지 않는다.
    """
    from . import config
    definition=snapshot['detail']['definitions']
    mode=snapshot.get('run',{}).get('mode')
    policy_exists=db.execute("SELECT 1 FROM sqlite_master WHERE name='release_policy' AND type='table'").fetchone()
    # explicit 실행은 release_policy 행은 있지만 z가 전부 NULL이다. 표 존재만 보면 안 된다.
    stored=policy_exists and not db.execute('SELECT 1 FROM release_policy WHERE due_z IS NULL LIMIT 1').fetchone()
    offsets=None
    if mode in ('parametric','explicit') and not stored:
        from .kpi_collector import input_hash
        expected=snapshot.get('meta',{}).get('input_hash')
        if not expected or expected==input_hash():
            offsets=due_offsets(config.XLSX_PATH)
        elif mode=='parametric' and not policy_exists:
            raise ValueError('입력 파일이 실행 시점과 다릅니다. 이전 결과의 z를 현재 시트로 복원하지 않습니다.')
        # 그 밖의 불일치는 z를 비워 둔다. 다른 워크북의 기준으로 나눈 z는 틀린 값이다.
    return analyze_database(db,warmup=definition['warmup_min'],end=definition['end_min'],offsets=offsets)


def plot_service(tables,path=None,product=None):
    import matplotlib.pyplot as plt
    service=tables['priority_service'];causes=tables['waiting_causes'];bands=tables['due_z_bands']
    if service.empty:return
    if product is None:
        urgent=service[service.lot_type=='superhot']
        product=urgent['product'].iloc[0] if not urgent.empty else service['product'].iloc[0]
    s=service[service['product']==product].set_index('lot_type').reindex(['regular','hot','superhot']).dropna(subset=['n'])
    fig,axes=plt.subplots(2,2,figsize=(15,10),constrained_layout=True)
    fig.suptitle(f'{product} | same-product priority service | completed new post-warmup lots\n'
                 'Rescoring keeps observed CT fixed; it is not a causal policy simulation')
    x=np.arange(len(s));width=.25
    for i,(column,label) in enumerate([('on_time_pct','Observed'),('z1_rescore_on_time_pct','Fixed z=1 rescore'),
                                      ('uniform_z_rescore_on_time_pct','Common U(0.8,1.2) rescore')]):
        axes[0,0].bar(x+(i-1)*width,s[column],width,label=label)
    axes[0,0].set(xticks=x,xticklabels=[f'{k}\nn={int(v)}' for k,v in zip(s.index,s.n)],ylim=(0,105),
                  title='On-time service and deadline sensitivity',ylabel='%')
    axes[0,0].legend(fontsize=8)
    axes[0,1].bar(x-.18,s.ct_mean_h,.36,label='Mean cycle time')
    axes[0,1].bar(x+.18,s.due_mean_h,.36,label='Mean allowed lead time')
    axes[0,1].set(xticks=x,xticklabels=s.index,title='Actual CT vs realized due allowance',ylabel='Hours')
    axes[0,1].legend()
    z=bands[bands['product']==product] if not bands.empty else bands
    if not z.empty:
        counts=z.pivot_table(index='z_band',columns='lot_type',values='n',aggfunc='sum',fill_value=0)
        (counts/counts.sum()*100).plot.bar(ax=axes[1,0],rot=0)
    axes[1,0].set(title='Realized z mix (completed cohort)',ylabel='% within lot type',xlabel='z band')
    reasons=causes[causes['product']==product].pivot_table(index='lot_type',columns='bucket',values='mean_h',fill_value=0)
    if not reasons.empty:reasons.reindex(s.index).plot.barh(stacked=True,ax=axes[1,1])
    axes[1,1].set(title='Mean waiting reasons',xlabel='Hours per completed lot',ylabel='')
    if path is None:return fig
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    fig.savefig(path,dpi=140,bbox_inches='tight');plt.close(fig)
