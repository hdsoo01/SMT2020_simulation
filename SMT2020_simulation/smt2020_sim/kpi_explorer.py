"""Read-only, on-demand notebook analyses. Raw history is aggregated in SQLite.

No simulation is started here. All windows are [start, end), in minutes.
Completed-lot distributions use release >= start and completion <= end.
Interval burdens include censored observations, clipped to the window.
"""
from __future__ import annotations

from collections import Counter,defaultdict
from dataclasses import dataclass, field
import json
import math
import hashlib
import heapq
import io
import gzip
import os
from pathlib import Path
import sqlite3
import tempfile
import time

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

from .lot import BUCKETS
from .kpi_collector import WAIT_BUCKETS

YEAR = 365 * 24 * 60
DIMS = {'area', 'toolgroup', 'station', 'product', 'lot_type', 'step_index'}


@dataclass
class Analysis:
    tables: dict[str, pd.DataFrame] = field(default_factory=dict)
    kind: str = 'bar'
    x: str | None = None
    y: str | None = None
    hue: str | None = None
    note: str = ''
    status: str = '준비됨'


@dataclass
class AnalysisGroup:
    results: dict[str,Analysis]
    tables: dict[str,pd.DataFrame]
    aliases: dict[tuple[str,str],str]


# Coalesce only tables describing the same quantity, even if unrelated metrics
# happen to have identical numeric values in a small sample.
TABLE_FAMILIES={
    'priority_service':'on_time_service','on_time':'on_time_service',
    'station_states':'station_states','stations':'station_states','cascading_usage':'station_states',
    'fill_and_wait':'batch_records','batches':'batch_records','mixed_batch_composition':'batch_records',
    'risk_lots':'current_wip','unformed_batch_wip':'current_wip',
}


def _table_covers(larger,smaller):
    """Same rows or a projection/subset; ignore presentation row ordering."""
    if not set(smaller.columns).issubset(larger.columns):return False
    if smaller.empty:return larger.empty and set(larger.columns)==set(smaller.columns)
    if len(larger)<len(smaller):return False
    columns=sorted(smaller.columns)
    def hashes(frame):
        frame=frame[columns].copy()
        for col in columns:
            if pd.api.types.is_numeric_dtype(frame[col]):
                frame[col]=pd.to_numeric(frame[col]).astype(float).round(9)
            else:
                frame[col]=frame[col].astype('string')
        return Counter(pd.util.hash_pandas_object(frame,index=False).to_numpy())
    available,requested=hashes(larger),hashes(smaller)
    return all(available[h]>=n for h,n in requested.items())


class UnavailableAnalysis(ValueError):
    """Requested history is outside the declared recording coverage."""


def summary(frame, groups, value):
    if frame.empty:
        return pd.DataFrame()
    frame = frame.dropna(subset=[value])
    if frame.empty:
        return pd.DataFrame()
    g = frame.groupby(groups, dropna=False)[value]
    out = g.agg(n='count', mean='mean', std='std', minimum='min', maximum='max')
    for q in (5, 25, 50, 75, 90, 95):
        out[f'p{q}'] = g.quantile(q / 100)
    return out.reset_index()


class Explorer:
    def __init__(self, snapshot, *, product=None, lot_types=None, toolgroup=None,
                 station=None, step=None, start=None, end=None,
                 bin_minutes=10080, top=15, compare_paths=(), output=None,
                 allow_heavy=False, use_cache=True):
        self.path = Path(snapshot).resolve()
        self.snap = json.loads(self.path.read_text(encoding='utf-8'))
        run = self.snap.get('run', {})
        definitions = self.snap.get('detail', {}).get('definitions', {})
        self.collection=definitions.get('collection',{'level':'full','full_visits':True})
        self.warmup = float(definitions.get('warmup_min', run.get('warmup_minutes', 0)))
        self.start = max(self.warmup, float(self.warmup if start is None else start))
        self.end = float(definitions.get('end_min', run.get('years', 0) * YEAR)
                         if end is None else end)
        observation_end = float(definitions.get('end_min', run.get('years', 0) * YEAR))
        if not (0 <= self.start < self.end <= observation_end):
            raise ValueError('기간은 0 <= start < end <= 관측 종료로 설정하세요.')
        if bin_minutes <= 0 or top <= 0:
            raise ValueError('bin_minutes와 top은 양수여야 합니다.')
        self.bin = float(bin_minutes)
        self.top = int(top)
        self.filters = dict(product=product, toolgroup=toolgroup, station=station,
                            step_index=step)
        self.types = list(lot_types or [])
        if set(self.types) - {'regular', 'hot', 'superhot'}:
            raise ValueError('lot_types는 regular/hot/superhot만 지정할 수 있습니다.')
        self.output = Path(output) if output else None
        self.compare_paths = [Path(p).resolve() for p in compare_paths]
        self.cache = {}
        self.allow_heavy = bool(allow_heavy)
        self.use_cache = bool(use_cache)
        self.cache_dir = self.path.parent / 'notebook_cache'
        self.source_signature = [str(self.path), self.path.stat().st_size, self.path.stat().st_mtime_ns]
        self.db = None
        value = self.snap.get('detail', {}).get('database')
        if value:
            path = Path(value)
            path = path if path.is_absolute() else self.path.parent / path
            if not path.is_file():
                raise FileNotFoundError(f'스냅샷이 참조하는 상세 DB가 없습니다: {path}')
            self.source_signature += [str(path.resolve()), path.stat().st_size, path.stat().st_mtime_ns]
            self.db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
            self.db.row_factory = sqlite3.Row
            self.db.execute('PRAGMA temp_store=FILE')
            self.db.execute('PRAGMA cache_size=-131072')
            self.db.executescript('''
                CREATE TEMP VIEW V AS SELECT v.*,l.lot_type,l.wafers,l.initial_wip,
                    l.priority FROM visits v JOIN lots l USING(lot_id);
                CREATE TEMP VIEW S AS SELECT s.*,v.lot_id,v.product,v.step_index,
                    v.area,v.toolgroup,l.lot_type,l.wafers,v.status AS visit_status
                    FROM segments s JOIN visits v USING(visit_id)
                    JOIN lots l USING(lot_id);
                CREATE TEMP VIEW occupancy_intervals AS SELECT station,toolgroup,
                    'occupancy' AS kind,start_min,end_min FROM S WHERE station IS NOT NULL;
            ''')

    def close(self):
        if self.db is not None:
            self.db.close()
            self.db = None

    def export_lot_summary(self, path):
        from .kpi_tables import export_lot_summary
        if self.db is None:
            raise ValueError('lot 요약 CSV에는 상세 SQLite DB가 필요합니다.')
        print('[lot CSV] 최종 시점은 저장된 lot 합계, 과거 시점은 상세 구간 교차 집계.',flush=True)
        started=reported=time.monotonic()
        def progress():
            nonlocal reported
            now=time.monotonic()
            if now-reported>=20:
                print(f'[lot CSV 집계 중] {now-started:.0f}초 경과 · 중단 가능',flush=True)
                reported=now
            return 0
        self.db.set_progress_handler(progress,100000)
        try:
            return export_lot_summary(self.db,path,progress=lambda n:print(f'[lot CSV] {n:,}행',flush=True),
                warmup=self.warmup,start=self.start,end=self.end,
                product=self.filters['product'],lot_types=self.types,toolgroup=self.filters['toolgroup'],
                station=self.filters['station'],step=self.filters['step_index'])
        finally:
            self.db.set_progress_handler(None,0)

    def has(self, table):
        return self.db is not None and self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None

    def query(self, sql, params=()):
        started = reported = time.monotonic()
        def progress():
            nonlocal reported
            now = time.monotonic()
            if now-reported >= 20:
                print(f'[집계 중] {now-started:.0f}초 경과 · 중단 가능 · 원본 DB는 읽기 전용', flush=True)
                reported = now
            return 0
        self.db.set_progress_handler(progress, 100000)
        try:
            return pd.read_sql_query(sql, self.db, params=params)
        finally:
            self.db.set_progress_handler(None, 0)

    def _cache_path(self, key):
        physical=key[0] in {'work-metrics-stream-v1','bins','interval'} or (
            key[0]=='analysis' and key[1] in {'E01','E02','E03','E04','E05','E06','E07'})
        filters={k:v for k,v in self.filters.items() if k in {'station','toolgroup'}} if physical else self.filters
        signature = ['analysis-v3', self.source_signature, self.start, self.end,
                     filters, [] if physical else self.types, self.bin, key]
        digest = hashlib.sha256(json.dumps(signature,sort_keys=True,default=str).encode()).hexdigest()
        return self.cache_dir / (digest+'.json')

    def _load_cached(self, key):
        if not self.use_cache or self.compare_paths:
            return None
        try:
            path=self._cache_path(key)
            if path.exists():data=json.loads(path.read_text(encoding='utf-8'))
            else:
                with gzip.open(path.with_suffix('.json.gz'),'rt',encoding='utf-8') as stream:
                    data=json.load(stream)
            return {name:pd.read_json(io.StringIO(value),orient='table') for name,value in data['tables'].items()}, data.get('meta',{})
        except (OSError,ValueError,KeyError,TypeError,EOFError):
            return None

    def _save_cached(self, key, tables, meta=None):
        # Cache compact results only, never duplicate million-row raw event histories.
        large_summary=key[0] in {'work-metrics-stream-v1','bins','time-flow-stream-v2','constraint-events-stream-v2'} or (
            key[0]=='analysis' and key[1] in {'E04','B07','Q04','E08'})
        limit=1000000 if large_summary else 200000
        if not self.use_cache or self.compare_paths or any(len(t)>limit for t in tables.values()):
            return
        target = self._cache_path(key)
        temp_path=None
        try:
            target.parent.mkdir(parents=True,exist_ok=True)
            data = dict(tables={name:t.to_json(orient='table',index=False,double_precision=15)
                               for name,t in tables.items()},meta=meta or {})
            compressed=sum(len(t) for t in tables.values())>100000
            if compressed:target=target.with_suffix('.json.gz')
            with tempfile.NamedTemporaryFile(mode='wb',dir=target.parent,suffix='.tmp',delete=False) as stream:
                temp_path=Path(stream.name)
                if compressed:
                    with gzip.GzipFile(fileobj=stream,mode='wb',compresslevel=1) as packed:
                        packed.write(json.dumps(data,ensure_ascii=False).encode('utf-8'))
                else:stream.write(json.dumps(data,ensure_ascii=False).encode('utf-8'))
            os.replace(temp_path,target)
        except (OSError,ValueError,TypeError):
            # A read-only project or unsupported dtype must not prevent analysis.
            return
        finally:
            if temp_path is not None and temp_path.exists():
                temp_path.unlink()

    def _original_window(self):
        original=self.snap.get('detail',{}).get('definitions',{})
        return self.start==original.get('warmup_min') and self.end==original.get('end_min')

    def _traces_cover(self):
        return self.collection.get('full_visits',True) or (bool(self.types) and set(self.types).issubset(self.collection.get('trace_types',[])))

    def _cube_scope(self):
        return self.collection.get('flow_cube',False) and self._original_window() and self.filters['station'] is None

    def _check_traces(self):
        if not self._traces_cover():
            raise UnavailableAnalysis('경량 수집은 전체 운영 요약과 긴급 lot 상세 이력을 보관합니다. '
                '이 상세 분석은 LOT_TYPES에 기록된 종류를 선택하거나 full 수집 결과가 필요합니다. 미수집 이력을 0으로 표시하지 않습니다.')

    def _snapshot_rows(self, groups):
        if not self._original_window() or len(groups)!=1:
            return None
        dimension=groups[0]
        if any(v is not None and k!=dimension for k,v in self.filters.items()):
            return None
        if self.types and dimension!='lot_type':
            return None
        rows=self.snap.get('detail',{}).get('flow',{}).get(dimension)
        if rows is None:
            return None
        return [(key,value) for key,value in rows.items()
                if (self.filters.get(dimension) is None or key==self.filters[dimension])
                and (not self.types or key in self.types)]

    def _visit_source(self, groups):
        need_types='lot_type' in groups or bool(self.types)
        source='visits v CROSS JOIN lots l ON l.lot_id=v.lot_id' if need_types else 'visits v'
        alias=lambda col:'l.'+col if col=='lot_type' else 'v.'+col
        fields=','.join(alias(g)+' AS '+g for g in groups)
        clauses=[];params=[]
        for col,value in self.filters.items():
            if value is not None:
                clauses.append(alias(col)+'=?');params.append(value)
        if self.types:
            clauses.append('l.lot_type IN ('+','.join('?' for _ in self.types)+')')
            params.extend(self.types)
        return source,fields,(' AND '+' AND '.join(clauses) if clauses else ''),params

    def conditions(self, alias='', *, available=DIMS):
        prefix = alias + '.' if alias else ''
        clauses, params = [], []
        for col, val in self.filters.items():
            if col in available and val is not None:
                clauses.append(prefix + col + '=?')
                params.append(val)
        if self.types and 'lot_type' in available:
            clauses.append(prefix + 'lot_type IN (' + ','.join('?' for _ in self.types) + ')')
            params.extend(self.types)
        return (' AND ' + ' AND '.join(clauses) if clauses else ''), params

    def lots(self):
        if 'lots' not in self.cache:
            if any(self.filters[k] is not None for k in ('toolgroup','station','step_index')):
                self._check_traces()
            cond, params = self.conditions('l', available={'product', 'lot_type'})
            extra, ep = self.conditions('v')
            if any(self.filters[k] is not None for k in ('toolgroup', 'station', 'step_index')):
                cond += ' AND l.lot_id IN (SELECT v.lot_id FROM V v WHERE v.start_min<? AND v.end_min>?' + extra + ')'
                params += [self.end,self.start]+ep
            definitions = self.has('lot_definitions')
            fields = ',d.raw_pt_min,d.rpt_paper_min,d.start_step_index,d.rework_span' if definitions else ''
            join = ' LEFT JOIN lot_definitions d USING(lot_id)' if definitions else ''
            frame = self.query('SELECT l.lot_id,l.product,l.lot_type,l.wafers,l.initial_wip,'
                'l.release_min,l.due_min,l.end_min,l.status,l.step_index,l.age_min,'
                'l.lateness_min,l.steps_executed,l.steps_skipped,l.rework_count' + fields +
                ' FROM lots l' + join + ' WHERE l.release_min<? AND l.end_min>=?' + cond,
                [self.end, self.start] + params)
            frame['ct_h'] = frame['age_min'] / 60
            for denominator, column in [('raw_pt_min', 'ff_raw'), ('rpt_paper_min', 'ff_paper')]:
                frame[column] = frame.age_min / frame[denominator].replace(0, np.nan) if denominator in frame else np.nan
            self.cache['lots'] = frame
        return self.cache['lots']

    def completed(self):
        f = self.lots()
        return f[(f.status == 'completed') & (f.release_min >= self.start) &
                 (f.end_min <= self.end) & (f.initial_wip == 0)].copy()

    def flow(self, groups=('toolgroup', 'lot_type')):
        groups = tuple(groups)
        if set(groups) - DIMS:
            raise ValueError('지원하지 않는 집계 축입니다.')
        key = ('flow', groups)
        if key not in self.cache:
            saved=self._load_cached(key)
            if saved is not None:
                self.cache[key]=saved[0]['flow']
                return self.cache[key].copy()
            stored=self._snapshot_rows(groups)
            if stored is not None:
                result=pd.DataFrame([{groups[0]:name,**row['bucket_lot_h']} for name,row in stored],
                                    columns=list(groups)+list(BUCKETS))
                for column,buckets in [('wait_lot_h',WAIT_BUCKETS),('elapsed_lot_h',BUCKETS)]:
                    result[column]=result[list(buckets)].sum(axis=1)
                result['mean_wip_lots']=result.elapsed_lot_h/((self.end-self.start)/60)
                result['mean_waiting_lots']=result.wait_lot_h/((self.end-self.start)/60)
                self.cache[key]=result
                return result.copy()
            cube=('product','step_index','toolgroup','lot_type')
            if self._cube_scope() and set(groups).issubset(cube):
                cond,params=self.conditions('c',available=set(cube))
                fields=','.join('c.'+g for g in groups)
                sums=','.join(f'SUM(c.{b}_min)/60 AS {b}' for b in BUCKETS)
                result=self.query(f'SELECT {fields},{sums} FROM flow_cube c WHERE 1=1 {cond} GROUP BY {fields}',params)
                result['wait_lot_h']=result[list(WAIT_BUCKETS)].sum(axis=1)
                result['elapsed_lot_h']=result[list(BUCKETS)].sum(axis=1)
                result['mean_wip_lots']=result.elapsed_lot_h/((self.end-self.start)/60)
                result['mean_waiting_lots']=result.wait_lot_h/((self.end-self.start)/60)
                self.cache[key]=result
                self._save_cached(key,{'flow':result})
                return result.copy()
            self._check_traces()
            if groups!=cube and set(groups).issubset(cube) and self.filters['station'] is None:
                # One compact cross table serves B03/B05/B06/Q02/Q03, including after a kernel restart.
                full=self.flow(cube)
                result=full.groupby(list(groups),as_index=False)[list(BUCKETS)].sum()
                result['wait_lot_h']=result[list(WAIT_BUCKETS)].sum(axis=1)
                result['elapsed_lot_h']=result[list(BUCKETS)].sum(axis=1)
                result['mean_wip_lots']=result.elapsed_lot_h/((self.end-self.start)/60)
                result['mean_waiting_lots']=result.wait_lot_h/((self.end-self.start)/60)
                self.cache[key]=result
                self._save_cached(key,{'flow':result})
                return result.copy()
            if 'station' not in groups and self.filters['station'] is None:
                from .kpi_streaming import flow_totals
                result=flow_totals(self,groups)
                self.cache[key]=result
                self._save_cached(key,{'flow':result})
                return result.copy()
            cond, params = self.conditions('s')
            if 'station' in groups:
                cond += ' AND s.station IS NOT NULL'
            fields = ','.join('s.' + g for g in groups)
            frame = self.query(f'SELECT {fields},s.bucket,'
                'SUM(MAX(0,MIN(s.end_min,?)-MAX(s.start_min,?)))/60 AS lot_h '
                'FROM S s WHERE s.start_min<? AND s.end_min>?' + cond +
                f' GROUP BY {fields},s.bucket', [self.end, self.start, self.end, self.start] + params)
            if frame.empty:
                result = pd.DataFrame(columns=list(groups) + list(BUCKETS) + ['wait_lot_h', 'elapsed_lot_h', 'mean_wip_lots'])
            else:
                result = frame.pivot_table(index=list(groups), columns='bucket', values='lot_h',
                                          aggfunc='sum', fill_value=0).reset_index()
                for b in BUCKETS:
                    if b not in result:
                        result[b] = 0.0
                result['wait_lot_h'] = result[list(WAIT_BUCKETS)].sum(axis=1)
                result['elapsed_lot_h'] = result[list(BUCKETS)].sum(axis=1)
                result['mean_wip_lots'] = result.elapsed_lot_h / ((self.end-self.start)/60)
                result['mean_waiting_lots'] = result.wait_lot_h / ((self.end-self.start)/60)
            self.cache[key] = result
            self._save_cached(key,{'flow':result})
        return self.cache[key].copy()

    def visit_wait(self, groups=('toolgroup', 'lot_type'), buckets=WAIT_BUCKETS, *, quantiles=True):
        cache_key=('wait',tuple(groups),tuple(buckets),quantiles,self.start,self.end)
        if quantiles:cache_key+=('tails-stream-v2',)
        if cache_key in self.cache:
            return self.cache[cache_key].copy()
        saved=self._load_cached(cache_key)
        if saved is not None:
            self.cache[cache_key]=saved[0]['wait']
            return self.cache[cache_key].copy()
        if quantiles:
            from .kpi_streaming import tails
            result,_=tails(self,tuple(groups),tuple(buckets))
            self.cache[cache_key]=result
            self._save_cached(cache_key,{'wait':result})
            return result.copy()
        stored=self._snapshot_rows(groups) if not quantiles and tuple(buckets)==tuple(WAIT_BUCKETS) and 'station' not in groups else None
        if stored is not None:
            result=pd.DataFrame([{groups[0]:name,'n':r['wait_observations'],'mean':r['wait_mean_h'],
                'std':r.get('wait_std_h'),'maximum':r['wait_max_h'],'minimum':np.nan,
                **{'p'+str(q):np.nan for q in (5,25,50,75,90,95)}} for name,r in stored],
                columns=list(groups)+['n','mean','std','maximum','minimum']+['p'+str(q) for q in (5,25,50,75,90,95)])
            self.cache[cache_key]=result
            return result.copy()
        source,fields,cond,params=self._visit_source(groups)
        if self._cube_scope() and not quantiles and tuple(buckets)==tuple(WAIT_BUCKETS) and 'station' not in groups:
            cond,params=self.conditions('c',available={'product','step_index','toolgroup','lot_type'})
            fields=','.join('c.'+g for g in groups)
            result=self.query(f'SELECT {fields},SUM(wait_n) AS n,'
                'SUM(wait_sum_min)/NULLIF(SUM(wait_n),0)/60 AS mean,MAX(wait_max_min)/60 AS maximum,'
                'SUM(wait_sq_min)/NULLIF(SUM(wait_n),0) AS mean_square_min '
                f'FROM flow_cube c WHERE 1=1 {cond} GROUP BY {fields}',params)
            for c in ('mean','mean_square_min','maximum'):result[c]=pd.to_numeric(result[c],errors='coerce')
            result.loc[result.n==0,'maximum']=np.nan
            result['std']=np.sqrt((result.mean_square_min-(result['mean']*60)**2).clip(lower=0))/60
            result=result.drop(columns='mean_square_min')
            result['minimum']=np.nan
            for q in (5,25,50,75,90,95):result['p'+str(q)]=np.nan
            self.cache[cache_key]=result
            return result.copy()
        self._check_traces()
        names = ','.join(groups)
        expr = '+'.join('v.' + b + '_min' for b in buckets)
        if not quantiles:
            grouping=','.join(('l.' if g=='lot_type' else 'v.')+g for g in groups)
            sql=f'SELECT {fields},COUNT(*) AS n,AVG(({expr})/60) AS mean,'\
                f'MAX(({expr})/60) AS maximum,MIN(({expr})/60) AS minimum FROM {source} '\
                f"WHERE v.status='completed' AND v.start_min>=? AND v.end_min<=? {cond} GROUP BY {grouping}"
            result=self.query(sql,[self.start,self.end]+params)
            for q in (5,25,50,75,90,95):
                result['p'+str(q)]=np.nan
            self.cache[cache_key]=result
            self._save_cached(cache_key,{'wait':result})
            return result.copy()
    def stations(self):
        if 'stations' not in self.cache:
            rows = [{'station': k, **v} for k, v in self.snap.get('detail', {}).get('stations', {}).items()]
            frame = pd.DataFrame(rows)
            for col in ('toolgroup', 'station'):
                if self.filters[col] is not None and col in frame:
                    frame = frame[frame[col] == self.filters[col]]
            # The aggregate station snapshot is valid only for its original window.
            original = self.snap['detail']['definitions']
            if self.start != original['warmup_min'] or self.end != original['end_min']:
                times = self.interval_totals('station_work')
                down = self.interval_totals('downtime')
                occupancy = self.interval_totals('occupancy_intervals')
                for column, table in [('util_pct', times), ('unavail_pct', down)]:
                    frame[column] = frame.station.map(table.set_index('station').union_h).fillna(0) / ((self.end-self.start)/60)*100
                frame['occupancy_pct'] = frame.station.map(occupancy.set_index('station').union_h).fillna(0) / ((self.end-self.start)/60)*100
                frame['two_job_time_h'] = np.nan
                frame['down_events'] = np.nan
                frame['pm_events'] = np.nan
            if 'two_job_time_h' in frame:
                frame['two_job_pct']=frame.two_job_time_h/((self.end-self.start)/60)*100
            self.cache['stations'] = frame
        return self.cache['stations'].copy()

    def wait_distribution(self):
        from .kpi_streaming import tails
        distribution,cohorts=tails(self,('toolgroup','lot_type'),WAIT_BUCKETS)
        return Analysis({'wait_h':distribution,'visit_cohorts':cohorts},'box',y='mean',note=
            '장비군×lot 종류별 완료 방문 대기시간(시간)입니다. 기간 안에 시작·종료한 방문만 분포에 넣습니다. '
            'P50/P90/P95는 기존 SQL과 같은 floor(n×q)+1번째 정확한 순서통계입니다. '
            '표는 P95 내림차순이며 큰 표의 그림은 상위 TOP개입니다. 상자=P25/P75, 수염=P5/P95. '
            'visit_cohorts는 종료에 미완료인 방문 수를 별도로 제공합니다. warm-up 이전 시작·skipped 방문은 제외합니다.')

    def stream(self, sql, params=()):
        started=reported=time.monotonic()
        def progress():
            nonlocal reported
            now=time.monotonic()
            if now-reported>=20:
                print(f'[상세 집계 중] {now-started:.0f}초 경과 · 중단 가능',flush=True)
                reported=now
            return 0
        self.db.set_progress_handler(progress,100000)
        try:
            for i,row in enumerate(self.db.execute(sql,params)):
                if i%10000==0:
                    progress()
                yield row
        finally:
            self.db.set_progress_handler(None,0)

    def interval_totals(self, table, split=False, kinds=None):
        cache_key=('interval',table,split,tuple(kinds or []),self.start,self.end)
        if cache_key in self.cache:
            return self.cache[cache_key].copy()
        cached=self._load_cached(cache_key)
        if cached is not None:
            self.cache[cache_key]=cached[0]['intervals']
            return self.cache[cache_key].copy()
        if table=='station_work' and (split or kinds or not self._original_window()):
            from .kpi_streaming import work_metrics
            metrics=work_metrics(self)
            result=metrics['categories' if split else 'total'].copy()
            if kinds:
                result=metrics['categories'][metrics['categories'].bucket.isin(kinds)].copy()
                if not split:
                    # Callers needing union of multiple categories should not sum overlaps.
                    if len(kinds)>1:raise ValueError('여러 작업 종류의 합집합은 전체 가동률 표를 사용하세요.')
                    result=result.drop(columns='bucket')
            return result
        if self._original_window() and not split and not kinds:
            column={'station_work':'util_pct','downtime':'unavail_pct','occupancy_intervals':'occupancy_pct'}.get(table)
            states=self.stations()
            if column and not states.empty and column in states:
                result=states[['station',column]].rename(columns={column:'union_h'})
                result['union_h']*=((self.end-self.start)/60)/100
                result['intervals']=np.nan
                result['job_or_event_h']=np.nan
                self.cache[cache_key]=result
                return result.copy()
        if table in {'station_work','occupancy_intervals'} and not self.collection.get('station_work',True):
            raise UnavailableAnalysis('경량 수집은 원래 관측기간의 정확한 장비 가동·점유·정지 요약을 유지합니다. '
                '작업 종류별 합집합이나 다른 기간의 물리적 시간은 full 작업 구간 이력이 필요합니다.')
        cond, params = self.conditions('w', available={'station','toolgroup'})
        discriminator = 'bucket' if table == 'station_work' else 'kind'
        if kinds:
            cond+=' AND w.'+discriminator+' IN ('+','.join('?' for _ in kinds)+')'
            params.extend(kinds)
        order = f'w.station,w.{discriminator},w.start_min,w.end_min' if split else 'w.station,w.start_min,w.end_min'
        cursor = self.stream(f'SELECT w.station,w.{discriminator},MAX(w.start_min,?),MIN(w.end_min,?) '
            f'FROM {table} w WHERE w.start_min<? AND w.end_min>?' + cond + ' ORDER BY ' + order,
            [self.start, self.end, self.end, self.start] + params)
        totals = defaultdict(lambda: [0., 0, 0.])
        previous, right = None, None
        for station, kind, a, b in cursor:
            key = (station, kind) if split else (station,)
            if key != previous:
                right, previous = None, key
            totals[key][0] += max(0., b-max(a, right)) if right is not None else b-a
            totals[key][1] += 1
            totals[key][2] += b-a
            right = max(right, b) if right is not None else b
        columns = ['station', discriminator] if split else ['station']
        self.cache[cache_key]=pd.DataFrame([list(k)+[v[0]/60,v[1],v[2]/60] for k,v in totals.items()],
                            columns=columns+['union_h','intervals','job_or_event_h'])
        self._save_cached(cache_key,{'intervals':self.cache[cache_key]})
        return self.cache[cache_key].copy()

    def batches(self):
        if 'batches' in self.cache:return self.cache['batches'].copy()
        cond, params = self.conditions('b', available={'product','toolgroup','station','step_index'})
        f = self.query('SELECT b.* FROM batches b WHERE b.start_min>=? AND b.start_min<?' + cond,
                       [self.start,self.end]+params)
        if self.types:
            ids = set(self.lots().lot_id)
            f = f[f.lot_ids_json.map(lambda value: bool(ids.intersection(json.loads(value))))]
        f['wait_mean_h'] = f.wait_mean_min/60
        f['wait_max_h'] = f.wait_max_min/60
        self.cache['batches']=f
        return f

    def events(self, prefix):
        if not (prefix=='reservation_' and 'superhot' in self.collection.get('trace_types',[])):
            self._check_traces()
        cond, params = self.conditions('l', available={'product','lot_type'})
        for col in ('toolgroup','station','step_index'):
            value=self.filters[col]
            if value is not None:
                cond+=f" AND json_extract(e.attributes_json,'$.{col}')=?"
                params.append(value)
        f = self.query('SELECT e.*,l.lot_type FROM events e JOIN lots l USING(lot_id) '
            'WHERE e.time_min>=? AND e.time_min<? AND e.kind LIKE ?'+cond,
            [self.start,self.end,prefix+'%']+params)
        if not f.empty:
            attrs = pd.json_normalize(f.attributes_json.map(json.loads))
            f = pd.concat([f.drop(columns='attributes_json'),attrs],axis=1)
        return f

    def waiting_wip(self):
        cache_key=('waiting_wip',self.end)
        if cache_key in self.cache:return self.cache[cache_key].copy()
        if self.end!=self.snap.get('detail',{}).get('definitions',{}).get('end_min'):
            self._check_traces()
        cond, params = self.conditions('v',available=DIMS-{'lot_type'})
        if self.types:
            cond+=' AND l.lot_type IN ('+','.join('?' for _ in self.types)+')'
            params.extend(self.types)
        origin_end=self.snap.get('detail',{}).get('definitions',{}).get('end_min')
        if self.end==origin_end:
            cond+=" AND l.status='wip'"
        # A historical endpoint is supported: the state covering end is selected.
        f = self.query('''SELECT v.lot_id,v.product,l.lot_type,v.step_index,v.toolgroup,
            s.station,s.bucket,(?-v.start_min)/60 AS visit_age_h,
            (?-s.start_min)/60 AS state_age_h,l.release_min,l.due_min,
            (? - l.release_min)/60 AS whole_age_h,(?-l.due_min)/60 AS overdue_h
            FROM lots l CROSS JOIN visits v ON v.lot_id=l.lot_id
            CROSS JOIN segments s ON s.visit_id=v.visit_id
            WHERE l.release_min<? AND l.end_min>=?
            AND s.start_min<? AND s.end_min>=? AND v.status!='skipped'
            AND (v.end_min>? OR (v.end_min=? AND v.status='censored'))
            AND l.initial_wip=0 AND l.release_min>=? '''+cond,
            [self.end]*10+[self.warmup]+params)
        # At an exact boundary the new interval owns the state, except final censoring.
        f = f.sort_values('state_age_h').drop_duplicates('lot_id',keep='first')
        self.cache[cache_key]=f
        return f

    def time_flow(self, groups=('toolgroup','lot_type')):
        from .kpi_streaming import time_flow
        return time_flow(self,tuple(groups))

    def missing(self, text):
        return Analysis(note=text,status='추가 데이터 필요')

    def analyze(self, code, *, exact_percentiles=False):
        key=('analysis',code,exact_percentiles,self.allow_heavy if code=='Q05' else None)
        if code=='Q01':key+=('cqt-stream-v1',self.top)
        if code=='B04':key+=('wait-stream-v1',)
        if code in {'B07','E03','E04','E08','C03','C06','Q03','Q04'}:
            key+=('bounded-stream-v2',self.top if code in {'Q03','Q04'} else None)
        if code in {'E01','E02'}:key+=('station-columns-v2',)
        if code=='P01':
            from . import config
            stat=config.XLSX_PATH.stat() if config.XLSX_PATH.exists() else None
            # z 역산 규칙이 바뀌면 태그를 올린다 — 옛 캐시의 빈 z 표를 다시 쓰지 않도록.
            key+=((stat.st_size,stat.st_mtime_ns) if stat else None,'z-explicit-v1')
        cached=self._load_cached(key)
        if cached is not None:
            tables,meta=cached
            return Analysis(tables,**meta)
        started=time.monotonic()
        print(f'[{code}] 저장된 요약/캐시 우선 조회',flush=True)
        try:
            result=self._analyze(code,exact_percentiles=exact_percentiles)
        except UnavailableAnalysis as exc:
            result=self.missing(str(exc))
        if result.status=='준비됨':
            meta={name:getattr(result,name) for name in ('kind','x','y','hue','note','status')}
            self._save_cached(key,result.tables,meta)
        print(f'[{code}] 완료 · {time.monotonic()-started:.2f}초',flush=True)
        return result

    def _analyze(self, code, *, exact_percentiles=False):
        if code=='P01':
            if self.db is None:return self.missing('제품×종류 납기 진단에는 lot 요약 DB가 필요합니다.')
            if not self._original_window() or any(self.filters[k] is not None for k in ('toolgroup','station','step_index')):
                return self.missing('P01은 원래 측정기간의 전체 fab lot 납기 진단입니다. 기간·장비/step 조건을 해제하고 제품/종류를 선택하세요.')
            from .priority_analysis import for_snapshot
            tables=for_snapshot(self.db,self.snap)
            if self.filters['product'] is not None:
                tables={k:v[v['product']==self.filters['product']] if 'product' in v else v for k,v in tables.items()}
            if self.types:
                tables={k:v[v['lot_type'].isin(self.types)] if 'lot_type' in v else v for k,v in tables.items()}
            if tables['priority_service'].empty:
                return Analysis(tables,note='선택 제품/종류에 warm-up 이후 신규 완료 lot 표본이 없습니다.',status='표본 없음')
            return Analysis(tables,'heat',x='product',y='lot_type',hue='on_time_pct',
                note='같은 제품끼리 납기·CT·z·대기를 비교합니다. z=1/공통 z 분포 및 대기 제거 수치는 관측 CT 고정 재채점이며 정책 변경의 인과 효과가 아닙니다. 납기 도래 신규 lot 분모와 미완료도 따로 표시합니다.')
        if code in {'X01','X02','X03'}:
            return self.comparison(code)
        if code == 'Q05':
            diagnostics = self.snap.get('counters',{})
            frame = pd.DataFrame([{'item':k,'value':str(v)} for k,v in diagnostics.items()])
            tables = {'model_diagnostics':frame}
            if self.db is not None:
                expr = '+'.join(b+'_min' for b in BUCKETS)
                if not self.collection.get('full_visits',True):
                    audit=self.snap['detail']['definitions'].get('visit_accounting',{})
                    tables['online_visit_accounting']=pd.DataFrame([audit])
                elif self.allow_heavy:
                    tables['visit_accounting'] = self.query(f'SELECT COUNT(*) AS visits,'
                        f'SUM(ABS(({expr})-(end_min-start_min))>1e-6) AS violations FROM visits')
                tables['lot_conservation'] = self.query("SELECT status,COUNT(*) AS lots,SUM(wafers) AS wafers FROM lots GROUP BY status")
            return Analysis(tables,note='모델 진단은 전체 실행 기준입니다. 경량 결과는 모든 방문의 온라인 시간 항등식을 표시합니다. full 이력 재검사는 ALLOW_HEAVY=True일 때 실행합니다.')
        if self.db is None:
            return self.json_only(code)
        if code.startswith('O'):
            return self.operations(code)
        if code.startswith('B'):
            return self.bottlenecks(code,exact_percentiles=exact_percentiles)
        if code.startswith('E'):
            return self.equipment(code)
        if code.startswith('C'):
            return self.batching(code)
        if code.startswith('Q'):
            return self.constraints(code)
        if code == 'X04':
            self.lots()  # Cache the whole selected window before temporarily splitting it.
            midpoint=(self.start+self.end)/2
            original=self.start,self.end
            frames=[]
            try:
                for period,a,b in [('first_half',original[0],midpoint),('second_half',midpoint,original[1])]:
                    self.start,self.end=a,b
                    f=self.completed()
                    t=summary(f,['product','lot_type'],'ct_h')
                    t['period']=period
                    frames.append(t)
            finally:
                self.start,self.end=original
            return Analysis({'ct_by_period':pd.concat(frames,ignore_index=True)},'bar',y='p95',
                            note='각 기간 안에서 투입·완료한 lot의 비교입니다. 정책 효과의 인과 추정은 아닙니다.')
        raise ValueError(f'알 수 없는 분석 ID: {code}')

    def json_only(self, code):
        ct=self.snap.get('cycle_time',{})
        if code in {'O05','O08'}:
            f=pd.DataFrame([{'lot_type':k,**v} for k,v in ct.get('spread_by_lot_type',{}).items()])
            if self.types and not f.empty:
                f=f[f.lot_type.isin(self.types)]
            return Analysis({'existing_type_ct':f},'box',note='이미 있는 종류별 CT 요약만 표시합니다. 제품×종류 교차 분석은 상세 DB가 필요합니다.',status='기존 요약만 가능')
        if code=='O06':
            f=pd.DataFrame([{'lot_type':k,**v} for k,v in ct.get('buckets_by_lot_type',{}).items()])
            return Analysis({'existing_type_buckets':f},'stack',note='이미 저장된 종류별 평균 시간입니다. 제품×종류 분석은 상세 DB가 필요합니다.',status='기존 요약만 가능')
        return self.missing('이 스냅샷에는 상세 SQLite 이력이 없습니다. 새 실행의 JSON을 선택하면 분석합니다.')

    def operations(self, code):
        f=self.completed()
        groups=['product','lot_type']
        if code in {'O01','O02'}:
            cache_key=('operation_time_flow',self.start,self.end,self.bin)
            if cache_key in self.cache:
                t=self.cache[cache_key].copy()
                return Analysis({'time_flow':t},'line',x='day',y='mean_wip_lots' if code=='O01' else 'completion_wspw',
                    note='생산 흐름과 출하량은 같은 시간별 표를 재사용합니다. 시간 bin의 실제 길이로 계산합니다.')
            lots=self.lots(); rows=[]
            for (p,l),g in lots.groupby(groups):
                for i,a in enumerate(np.arange(self.start,self.end,self.bin)):
                    b=min(a+self.bin,self.end)
                    arrivals=g[(g.release_min>=a)&(g.release_min<b)&(g.initial_wip==0)]
                    departures=g[(g.status=='completed')&(g.end_min>=a)&(g.end_min<b)]
                    clipped=(np.minimum(g.end_min,b)-np.maximum(g.release_min,a)).clip(lower=0)
                    rows.append(dict(product=p,lot_type=l,day=a/1440,release_lots=len(arrivals),
                        completion_lots=len(departures),release_wafers=arrivals.wafers.sum(),
                        completion_wafers=departures.wafers.sum(),period_start_min=a,
                        period_end_min=b,period_hours=(b-a)/60,
                        release_wspw=arrivals.wafers.sum()*10080/(b-a),
                        completion_wspw=departures.wafers.sum()*10080/(b-a),
                        mean_wip_lots=clipped.sum()/(b-a)))
            t=pd.DataFrame(rows)
            self.cache[cache_key]=t
            return Analysis({'time_flow':t},'line',x='day',y='mean_wip_lots' if code=='O01' else 'completion_wspw',
                note='WIP는 시간가중 평균이며 각 bin의 실제 길이를 사용합니다. 장비군/step 필터 시 해당 구간을 방문한 lot의 전체 fab 흐름입니다.')
        if code=='O03':
            f['on_time']=f.end_min<=f.due_min
            t=f.groupby(groups).agg(n=('lot_id','size'),on_time_pct=('on_time',lambda s:s.mean()*100)).reset_index()
            w=self.waiting_wip(); w=w[w.overdue_h>0]
            return Analysis({'on_time':t,'overdue_wip':w},'heat',x='product',y='lot_type',hue='on_time_pct')
        if code=='O04':
            f['late_h']=f.lateness_min.clip(lower=0)/60
            s=summary(f[f.late_h>0],groups,'late_h')
            frequency=f.assign(late=f.late_h>0).groupby(groups).agg(completed_n=('lot_id','size'),late_pct=('late',lambda s:s.mean()*100)).reset_index()
            return Analysis({'positive_lateness':s,'late_frequency':frequency},'box',note='지연시간 분포의 n은 지연 lot만입니다. 납기 내 lot은 빈도 표에 포함합니다.')
        if code in {'O05','O08'}:
            tables={'ct_h':summary(f,groups,'ct_h'),'ff_raw':summary(f,groups,'ff_raw'),
                    'ff_paper':summary(f,groups,'ff_paper')}
            note='n을 함께 비교하세요. FF raw와 FF paper는 분모가 다릅니다. 종류 간 관측 차이는 정책의 인과 효과가 아닙니다.'
            if not self.has('lot_definitions'):
                note+=' 이 DB에는 FF 분모가 없어 새 FF 분포는 표시하지 않습니다.'
            if code=='O08':
                t=tables['ct_h'].copy()
                if t.empty:
                    return Analysis({'priority_service_difference':pd.DataFrame(),**tables},
                        note='선택 조건에 신규 완료 lot 표본이 없습니다. 우선순위 효과를 0으로 해석하지 마세요.',status='표본 없음')
                regular=t[t.lot_type=='regular'][['product','mean','p95']].rename(columns={'mean':'regular_mean','p95':'regular_p95'})
                t=t.merge(regular,on='product',how='left')
                t['mean_vs_regular_pct']=(t['mean']/t.regular_mean-1)*100
                t['p95_vs_regular_pct']=(t.p95/t.regular_p95-1)*100
                tables={'priority_service_difference':t,**tables}
                return Analysis(tables,y='p95_vs_regular_pct',note=note+' regular 기준 꼬리 차이(%)입니다. 음수는 짧은 CT입니다.')
            return Analysis(tables,'box',note=note)
        if code=='O06':
            cond,params=self.conditions('l',available={'product','lot_type'})
            columns=','.join(f"AVG(json_extract(l.buckets_json,'$.{b}'))/60 AS {b}" for b in BUCKETS)
            t=self.query(f'SELECT l.product,l.lot_type,COUNT(*) AS n,{columns} FROM lots l '
                "WHERE l.status='completed' AND l.initial_wip=0 AND l.release_min>=? AND l.end_min<=?"+cond+
                ' GROUP BY l.product,l.lot_type',[self.start,self.end]+params)
            if any(self.filters[k] is not None for k in ('toolgroup','station','step_index')):
                return self.missing('완료 lot 전체 CT 구성입니다. toolgroup/station/step 필터는 해제하거나 B03의 방문별 대기 구성을 사용하세요.')
            return Analysis({'mean_ct_buckets_h':t},'stack',note='완료 lot 평균시간입니다. setup/LU를 순수 처리시간으로 합치지 않습니다.')
        if code=='O07':
            w=self.waiting_wip()
            return Analysis({'wip_age':summary(w,groups,'whole_age_h'),
                'risk_lots':w.sort_values('whole_age_h',ascending=False)},y='whole_age_h',
                note='전체 fab 나이, 현재 방문 나이, 현재 상태 나이를 구분합니다. 음수 overdue_h는 아직 납기 전입니다.')
        raise ValueError(code)

    def bottlenecks(self, code, *, exact_percentiles=False):
        if code=='B01':
            f=self.flow(('toolgroup',))
            w=self.visit_wait(('toolgroup',),quantiles=exact_percentiles)
            s=self.stations().groupby('toolgroup').agg(util_pct=('util_pct','mean'),
                occupancy_pct=('occupancy_pct','mean'),unavail_pct=('unavail_pct','mean'),tools=('station','size')).reset_index()
            t=f.merge(w,on='toolgroup',how='left').merge(s,on='toolgroup',how='left')
            return Analysis({'bottleneck_candidates':t.sort_values('wait_lot_h',ascending=False)},y='wait_lot_h',
                note='순위는 대기 부담 기준의 조사 후보입니다. 높은 가동률만으로 병목을 확정하지 않습니다. '
                     +('정확한 분위수를 계산했습니다.' if exact_percentiles else
                       '기본은 저장된 평균·최대 대기와 장비 상태를 사용합니다. P95 등 분위수는 미계산(빈칸)입니다. '
                       '필요하면 explorer.show("B01", exact_percentiles=True)로 선택 계산하세요.'))
        if code=='B02':
            r=self.bottlenecks('B01',exact_percentiles=exact_percentiles)
            r.kind,r.x,r.y='scatter','util_pct','wait_lot_h'
            return r
        if code=='B03':
            f=self.flow(('toolgroup','lot_type'))
            return Analysis({'waiting_reasons_lot_h':f[['toolgroup','lot_type']+list(WAIT_BUCKETS)]},'stack',
                note='미완료 방문도 포함한 관측 구간 대기 부담입니다. 완료 방문 대기 평균과 모집단이 다릅니다.')
        if code=='B04':
            return self.wait_distribution()
        if code=='B05':
            f=self.flow(('product','toolgroup','lot_type'))
            return Analysis({'product_group_wait':f},'heat',x='toolgroup',y='product',hue='wait_lot_h',
                note='종류를 선택하면 그 종류의 열지도입니다. 전체 종류일 때 그림은 합계, 표는 종류별입니다.')
        if code=='B06':
            f=self.flow(('product','step_index','toolgroup','lot_type'))
            return Analysis({'route_profile':f.sort_values(['product','step_index','lot_type'])},'line',x='step_index',y='wait_lot_h',
                note='route index는 0부터입니다. 제품을 선택하면 순서대로 읽기 쉽습니다. 같은 step의 재방문도 포함합니다.')
        if code=='B07':
            f=self.time_flow()
            return Analysis({'absolute_waiting_timeline':f},'heat',x='day',y='toolgroup',hue='mean_waiting_lots',
                note='모든 행에 동일한 절대 색 척도를 사용합니다. 단위는 bin 내 시간가중 평균 대기 lot 수입니다.')
        if code=='B08':
            if self._cube_scope():
                cond,params=self.conditions('c',available={'product','step_index','toolgroup','lot_type'})
                f=self.query('SELECT product,step_index,toolgroup,lot_type,'
                    'SUM(completed+skipped) AS visits,SUM(skipped) AS skips,SUM(reworks) AS reworks '
                    'FROM flow_cube c WHERE 1=1'+cond+' GROUP BY product,step_index,toolgroup,lot_type',params)
                return Analysis({'rework_and_skips':f},y='reworks',note='모든 lot의 공정별 온라인 집계입니다. 재작업은 불량률이 아닙니다.')
            self._check_traces()
            source,fields,cond,params=self._visit_source(('product','step_index','toolgroup','lot_type'))
            f=self.query(f'SELECT {fields},COUNT(*) AS visits,'
                "SUM(v.status='skipped') AS skips,SUM(v.rework_target IS NOT NULL) AS reworks "
                f'FROM {source} WHERE v.end_min>=? AND v.end_min<? AND v.status!=\'censored\''+cond+
                ' GROUP BY v.product,v.step_index,v.toolgroup,l.lot_type',[self.start,self.end]+params)
            return Analysis({'rework_and_skips':f.sort_values('reworks',ascending=False)},y='reworks',
                note='재작업 횟수는 wafer 불량률이 아닙니다. 반복 방문 시간은 flow 표에 포함됩니다.')
        raise ValueError(code)

    def equipment(self, code):
        note='장비 지표는 물리적 시간입니다. 제품/종류 필터는 장비 전체 가동률을 바꾸지 않습니다. 종류별 자원 배분은 E08을 사용하세요.'
        if code=='E01':
            f=self.stations().sort_values('util_pct',ascending=False)
            return Analysis({'station_states':f},y='occupancy_pct',note=note+' 가동률 막대는 기존 F03에서 이미 제공됩니다. 여기서는 점유·비가용도 함께 확인합니다.')
        if code=='E02':
            f=self.stations()
            t=f.groupby('toolgroup').agg(tools=('station','size'),mean_util=('util_pct','mean'),
                min_util=('util_pct','min'),max_util=('util_pct','max'),std_util=('util_pct','std')).reset_index()
            t['range_pct']=t.max_util-t.min_util
            return Analysis({'intragroup_range':t.sort_values('range_pct',ascending=False),'stations':f},y='range_pct',note=note)
        if code=='E03':
            f=self.interval_totals('station_work',split=True)
            f['union_pct']=f.union_h/((self.end-self.start)/60)*100
            return Analysis({'working_categories':f},y='union_pct',note=note+' 각 작업 종류의 합집합입니다. 종류끼리 겹칠 수 있어 비율을 단순 누적하지 않습니다.')
        if code=='E04':
            cond,params=self.conditions('w',available={'toolgroup','station'})
            # Bounded detailed view. Select a station and a short time window for a useful Gantt.
            from .kpi_streaming import work_metrics
            f=work_metrics(self)['gantt']
            down=self.query("SELECT station,kind AS bucket,MAX(start_min,?) AS start_min,MIN(end_min,?) AS end_min "
                'FROM downtime w WHERE start_min<? AND end_min>?'+cond+' ORDER BY start_min LIMIT 400',
                [self.start,self.end,self.end,self.start]+params)
            work_bins=self.interval_bins('station_work').rename(columns={'union_pct':'util_pct'})
            down_bins=self.interval_bins('downtime').rename(columns={'union_pct':'unavail_pct'})
            bin_keys=['station','day','period_start_min','period_end_min','period_hours']
            bins=work_bins.merge(down_bins,on=bin_keys,how='outer').fillna(0)
            return Analysis({'station_time_bins':bins,'gantt':pd.concat([f,down],ignore_index=True)},'heat',
                x='day',y='station',hue='util_pct',
                note=note+' 타임라인은 작업·정지 각각 최대 400구간입니다. 전체 이력으로 오해하지 않도록 장비와 짧은 기간을 선택하세요.')
        if code in {'E05','E06'}:
            cond,params=self.conditions('d',available={'station','toolgroup'})
            f=self.query('SELECT * FROM downtime d WHERE d.start_min<? AND d.end_min>?'+cond,
                         [self.end,self.start]+params)
            f['observed_h']=(f.end_min.clip(upper=self.end)-f.start_min.clip(lower=self.start))/60
            if code=='E05':
                g=f[f.kind=='breakdown'].sort_values(['station','start_min']).copy()
                g['start_gap_h']=g.groupby('station').start_min.diff()/60
                s=summary(g[(g.status=='completed')&(g.start_min>=self.start)&(g.end_min<=self.end)],['toolgroup','station'],'observed_h')
                return Analysis({'complete_failure_duration_h':s,'failure_events':g},'box',
                    note=note+' 시작 간격은 달력시간이며 MTBF로 단정하지 않습니다. 경계·미종료 고장은 이벤트 표에 분리합니다.')
            pm=f[f.kind=='pm'].groupby(['toolgroup','station','name']).agg(events=('event_id','size'),event_h=('observed_h','sum')).reset_index()
            all_union=self.interval_totals('downtime')
            by_kind=self.interval_totals('downtime',split=True)
            t=by_kind.pivot_table(index='station',columns='kind',values='union_h',fill_value=0).reset_index()
            t=t.merge(all_union[['station','union_h']],on='station',how='left')
            t['breakdown_pm_overlap_h']=t.get('breakdown',0)+t.get('pm',0)-t.union_h
            return Analysis({'pm_streams':pm,'union_and_overlap':t},y='event_h',note=note+' PM 이벤트 시간 합계와 전체 비가용 합집합을 구분합니다.')
        if code=='E07':
            f=self.stations()
            f=f[f.capacity>1].copy()
            f['two_job_pct']=f.two_job_time_h/((self.end-self.start)/60)*100
            return Analysis({'cascading_usage':f},y='two_job_pct',note=note+' 2-job 비율은 원래 warm-up 관측 구간에서만 제공됩니다.')
        if code=='E08':
            return self.resource_allocation()
        raise ValueError(code)

    def interval_bins(self, table):
        if table=='station_work':
            from .kpi_streaming import work_metrics
            return work_metrics(self)['bins'].copy()
        if table=='station_work' and not self.collection.get('station_work',True):
            raise UnavailableAnalysis('장비의 구간별 가동률과 Gantt는 full 작업 구간 이력이 필요합니다. 경량 결과의 E01/E02/E07은 유지됩니다.')
        key=('bins',table,self.start,self.end,self.bin)
        if key in self.cache:
            return self.cache[key].copy()
        cached=self._load_cached(key)
        if cached is not None:
            self.cache[key]=cached[0]['bins']
            return self.cache[key].copy()
        cond,params=self.conditions('w',available={'station','toolgroup'})
        cursor=self.stream(f'SELECT station,MAX(start_min,?),MIN(end_min,?) FROM {table} w '
            'WHERE start_min<? AND end_min>?'+cond+' ORDER BY station,start_min,end_min',
            [self.start,self.end,self.end,self.start]+params)
        totals=defaultdict(float)
        def add_interval(st,a,b):
            first=int((a-self.start)//self.bin)
            last=int(np.nextafter((b-self.start)/self.bin,-np.inf))
            for i in range(first,last+1):
                left=self.start+i*self.bin;right=min(left+self.bin,self.end)
                totals[(st,i)]+=max(0,min(b,right)-max(a,left))
        previous=None;a=b=None
        for st,left,right in cursor:
            if st!=previous or (b is not None and left>b):
                if previous is not None:
                    add_interval(previous,a,b)
                previous,a,b=st,left,right
            else:
                b=max(b,right)
        if previous is not None:
            add_interval(previous,a,b)
        rows=[]
        stations={k[0] for k in totals}
        for st,info in self.snap.get('detail',{}).get('stations',{}).items():
            if self.filters['station'] is not None and st!=self.filters['station']:
                continue
            if self.filters['toolgroup'] is not None and info['toolgroup']!=self.filters['toolgroup']:
                continue
            stations.add(st)
        for st in sorted(stations):
            for i,left in enumerate(np.arange(self.start,self.end,self.bin)):
                right=min(left+self.bin,self.end);span=right-left
                rows.append(dict(station=st,day=left/1440,union_pct=100*totals[(st,i)]/span,
                    period_start_min=left,period_end_min=right,period_hours=span/60))
        self.cache[key]=pd.DataFrame(rows,columns=['station','day','union_pct','period_start_min','period_end_min','period_hours'])
        self._save_cached(key,{'bins':self.cache[key]})
        return self.cache[key].copy()

    def resource_allocation(self):
        if not self.collection.get('work_members',True):
            return self.missing('경량 수집에서는 work_members를 생략합니다. 종류별 물리적 장비시간 배분은 full 수집 결과가 필요합니다.')
        if not self.has('work_members'):
            return self.missing('종류별 자원 배분은 schema 3의 work_members 연결이 필요합니다.')
        from .kpi_streaming import allocation
        frame,unknown=allocation(self)
        return Analysis({'physical_work_allocation':frame},y='allocated_h',note=
            '동시 작업에는 장비시간을 균등 배분하고, 배치 구성원에는 wafer 수로 배분합니다. '
            '전체 배분 합은 작업 합집합과 같습니다. 인과적 필요 용량은 아닙니다. '
            f'미연결 시간: {unknown:.6f} h. 시작 구간을 순차 처리하며 전체 이벤트 정렬을 하지 않습니다.')

    def batching(self, code):
        if code=='C06':
            return self.batch_wait_split()
        if code=='C03':
            groups=('product','step_index','toolgroup','lot_type')
            from .kpi_streaming import tails
            t,cohorts=tails(self,groups,('wait_batch',),batch_only=True)
            return Analysis({'member_batch_wait_h':t,'batch_visit_cohorts':cohorts},'box',y='mean',
                note='실제 batch ID가 있는 구성원 방문만 집계합니다. 물리적 배치 수가 아닙니다. '
                'warm-up 이전 시작·미완료 방문은 분포에서 제외합니다. 미배정 배치 대기 WIP는 C05입니다.')
        if code=='C05':
            f=self.waiting_wip(); f=f[f.bucket=='wait_batch']
            return Analysis({'unformed_batch_wip':f.sort_values('state_age_h',ascending=False)},y='state_age_h')
        f=self.batches()
        if code=='C01':
            return Analysis({'fill_and_wait':f},'scatter',x='fill_pct',y='wait_mean_h',
                note='물리적 batch당 한 점입니다. 종류 필터는 해당 종류 구성원이 있는 batch를 선택하며 batch 전체 대기 평균은 유지합니다.')
        if code=='C02':
            return Analysis({'fill_distribution':summary(f,['product','step_index','toolgroup'],'fill_pct'),'batches':f},'box')
        if code=='C04':
            lookup=self.query('SELECT lot_id,lot_type FROM lots').set_index('lot_id').lot_type.to_dict()
            for kind in ('regular','hot','superhot'):
                f[kind+'_members']=f.lot_ids_json.map(lambda ids:sum(lookup.get(l)==kind for l in json.loads(ids)))
            f['urgent_members']=f.hot_members+f.superhot_members
            return Analysis({'mixed_batch_composition':f},'scatter',x='fill_pct',y='wait_max_h',hue='urgent_members',
                note='batch는 중복 집계하지 않습니다. 종류별 구성원 수를 함께 보세요. 긴급 lot 구조 효과의 인과 증명은 아닙니다.')
        raise ValueError(code)

    def batch_wait_split(self):
        if self.collection.get('batch_wait_summary',False) and self._original_window() and self.filters['station'] is None:
            cond,params=self.conditions('c',available={'product','step_index','toolgroup','lot_type'})
            f=self.query('SELECT product,step_index,toolgroup,lot_type,'
                'quantity_short_min/60 AS quantity_short_lot_h,quantity_ready_min/60 AS quantity_ready_lot_h '
                'FROM batch_wait_summary c WHERE 1=1'+cond,params)
            return Analysis({'batch_quantity_wait_split':f},'stack',
                note='모든 lot의 온라인 수량 미충족/충족 후 대기 집계입니다. 충족 후 대기는 순수 장비 부족으로 단정하지 않습니다.')
        if not self.collection.get('batch_queue_state',True):
            return self.missing('경량 수집은 배치별 충진율·대기와 모든 lot의 배치 대기 총량을 유지합니다. 수량 충족 전후의 분리는 full 상태 이력이 필요합니다.')
        if not self.has('batch_queue_state'):
            return self.missing('배치 수량 충족 상태 이력은 schema 3 실행이 필요합니다.')
        from .kpi_streaming import quantity_split
        frame=quantity_split(self)
        return Analysis({'batch_quantity_wait_split':frame},'stack',note=
            '수량 상태의 임계값 변화와 누적 시간을 이용한 정확한 구간 적분입니다. '
            '수량 미충족/충족 후 대기를 분리하며 충족 후에는 장비·dispatch·setup·예약 제약이 포함됩니다. '
            'warm-up과 조회 경계를 잘라 계산하고 미완료 대기도 포함합니다.')

    def cqt_analysis(self):
        """Stream closed windows; retain excess values and a bounded example list.

        Never materialize all open/close events or normalize millions of JSON rows
        into a DataFrame. The compact result uses the normal persistent cache.
        """
        self._check_traces()
        if not self.has('events'):
            return self.missing('CQT 상세 이벤트가 없습니다.')
        kinds=dict(self.db.execute('SELECT lot_id,lot_type FROM lots'))
        sql="SELECT event_id,lot_id,product,time_min,attributes_json FROM events e WHERE kind='cqt_close' AND time_min>=? AND time_min<?"
        params=[self.start,self.end]
        if self.filters['product'] is not None:
            sql+=' AND product=?';params.append(self.filters['product'])
        if self.types:
            sql+=' AND lot_id IN (SELECT lot_id FROM lots WHERE lot_type IN ('+','.join('?' for _ in self.types)+'))'
            params.extend(self.types)
        for col in ('toolgroup','station','step_index'):
            if self.filters[col] is not None:
                sql+=f" AND json_extract(attributes_json,'$.{col}')=?"
                params.append(self.filters[col])
        groups={};examples=[];seen=excluded=0
        started=reported=time.monotonic()
        def progress():
            nonlocal reported
            now=time.monotonic()
            if now-reported>=20:
                print(f'[Q01 CQT 집계] {seen:,}개 확인 · {now-started:.0f}초 · 중단 가능',flush=True)
                reported=now
            return 0
        print('[Q01] 닫힌 CQT만 순차 집계합니다. 최초 조회 후 작은 결과 캐시를 재사용합니다.',flush=True)
        self.db.set_progress_handler(progress,100000)
        try:
            cursor=self.db.execute(sql,params)
            while rows:=cursor.fetchmany(5000):
                for event_id,lot_id,product,closed_at,raw in rows:
                    seen+=1
                    attrs=json.loads(raw)
                    elapsed=float(attrs['elapsed_min']);limit=float(attrs['limit_min'])
                    opened=float(attrs.get('start_min',closed_at-elapsed))
                    # A CQT cannot be clipped without changing its constraint.
                    # Exclude windows that began before the requested interval.
                    if opened<self.start:
                        excluded+=1;continue
                    kind=kinds[lot_id]
                    key=(product,kind,attrs['source_index'],attrs['target_index'])
                    g=groups.setdefault(key,dict(closed=0,violated=0,elapsed=0.,limit=limit,excess=[]))
                    g['closed']+=1;g['elapsed']+=elapsed
                    excess=max(0.,elapsed-limit)
                    if excess>0:
                        g['violated']+=1;g['excess'].append(excess/60)
                        example=dict(event_id=event_id,lot_id=lot_id,product=product,lot_type=kind,
                            source_index=key[2],target_index=key[3],source_end_min=opened,
                            target_start_min=closed_at,limit_h=limit/60,elapsed_h=elapsed/60,
                            excess_h=excess/60,toolgroup=attrs.get('toolgroup'),station=attrs.get('station'))
                        item=(excess,event_id,example)
                        if len(examples)<self.top:heapq.heappush(examples,item)
                        elif item[:2]>examples[0][:2]:heapq.heapreplace(examples,item)
                progress()
        finally:
            self.db.set_progress_handler(None,0)
        closed=[];positive=[]
        labels=('product','lot_type','source_index','target_index')
        for key,g in groups.items():
            identity=dict(zip(labels,key));values=np.asarray(g['excess'])
            closed.append(dict(**identity,closed=g['closed'],violated=g['violated'],
                violation_pct=100*g['violated']/g['closed'],limit_h=g['limit']/60,
                mean_elapsed_h=g['elapsed']/g['closed']/60))
            if len(values):
                record=dict(**identity,n=len(values),mean=values.mean(),
                    std=values.std(ddof=1) if len(values)>1 else np.nan,
                    minimum=values.min(),maximum=values.max())
                record.update({f'p{q}':v for q,v in zip((5,25,50,75,90,95),np.quantile(values,np.array([5,25,50,75,90,95])/100))})
                positive.append(record)
        total=sum(g['closed'] for g in groups.values());violations=sum(g['violated'] for g in groups.values())
        tables={'cqt_closed':pd.DataFrame(closed,columns=[*labels,'closed','violated','violation_pct','limit_h','mean_elapsed_h']),
            'positive_excess_h':pd.DataFrame(positive),
            'cqt_scope':pd.DataFrame([dict(closed=total,violated=violations,
                violation_pct=100*violations/total if total else np.nan,
                carry_in_windows_excluded=excluded,start_min=self.start,end_min=self.end)]),
            'cqt_events':pd.DataFrame([item[2] for item in sorted(examples,reverse=True)])}
        return Analysis(tables,y='violation_pct',
            note=f'CQT 시작·종료가 선택 기간 안인 닫힌 window만 집계합니다. carry-in {excluded:,}개 제외. '
            f'초과시간 분위수는 초과 window의 정확한 값입니다. cqt_events는 초과시간 상위 {self.top}개 사례이며 전체 이벤트가 아닙니다. '
            '전체 실행 카운터(warm-up 포함)와 분모가 다릅니다. Drop·미종료 window는 닫힌 분모에 넣지 않습니다.')

    def constraints(self, code):
        if code=='Q01':
            return self.cqt_analysis()
        if code=='Q02':
            f=self.events('reservation_')
            burden=self.flow(('toolgroup','lot_type'))[['toolgroup','lot_type','wait_reserved']]
            return Analysis({'reservation_events':f,'blocked_lot_h':burden},y='wait_reserved',
                note='예약 보유와 다른 lot의 차단 대기는 다른 값입니다. 인과 효과는 예약 ON/OFF 비교 실행으로 검토하세요.')
        if code=='Q03':
            f=self.flow(('product','step_index','toolgroup','lot_type'))
            from .kpi_streaming import event_summary
            counts,examples=event_summary(self,'dedication_target')
            return Analysis({'constraint_wait_lot_h':f[['product','step_index','toolgroup','lot_type','wait_gate','wait_dedicated']],
                'dedication_target_counts':counts,'dedication_targets':examples},'stack',
                note=f'제약 대기는 공통 flow 표를 재사용합니다. 지정 이벤트는 전체 빈도와 처음 {self.top}개 사례를 표시합니다. '
                     '배정 전 대기는 미래 배정 장비의 책임으로 소급하지 않습니다.')
        if code=='Q04':
            from .kpi_streaming import event_summary
            t,examples=event_summary(self,'setup_')
            actual=self.interval_totals('station_work',split=True,kinds=('setup_time',))
            actual=actual[actual.bucket=='setup_time']
            return Analysis({'setup_transition_plan':t,'actual_station_setup':actual,'setup_examples':examples},y='planned_min',
                note=f'전체 전환 횟수·계획시간을 순차 집계하며 원본 예시는 처음 {self.top}개입니다. '
                     '계획시간은 정지 포함 경과시간과 다릅니다. 실제 장비 setup 합집합은 공통 작업 집계를 재사용합니다.')
        raise ValueError(code)

    def comparison(self, code):
        paths=list(dict.fromkeys([self.path]+self.compare_paths))
        rows=[];type_rows=[]
        for p in paths:
            snap=json.loads(p.read_text(encoding='utf-8'))
            r=snap.get('run',{});m=snap.get('meta',{});w=snap.get('wip',{})
            rows.append(dict(file=p.name,seed=r.get('seed'),mode=r.get('mode'),years=r.get('years'),
                warmup_minutes=r.get('warmup_minutes'),model_hash=m.get('model_hash'),input_hash=m.get('input_hash'),
                load=r.get('load'),release_wspw=snap.get('throughput',{}).get('wspw_releases'),
                completion_wspw=snap.get('throughput',{}).get('wspw_completions'),mean_wip=w.get('mean_lots'),
                ff_p50=w.get('x_factor_p50'),ff_p90=w.get('x_factor_p90'),
                on_time_pct=snap.get('due_date',{}).get('on_time_pct')))
            for product,types in snap.get('cycle_time',{}).get('ff_by_product_type',{}).items():
                for lot_type,ff in types.items():
                    if self.filters['product'] is not None and product!=self.filters['product']:
                        continue
                    if self.types and lot_type not in self.types:
                        continue
                    type_rows.append(dict(file=p.name,product=product,lot_type=lot_type,
                        ff_mean=ff,n_type_all_products=snap.get('cycle_time',{}).get('count_by_lot_type',{}).get(lot_type)))
        f=pd.DataFrame(rows)
        type_table=pd.DataFrame(type_rows)
        if len(paths)<2:
            return Analysis({'run_manifest':f,'product_type_ff':type_table},note='COMPARE_SNAPSHOTS에 비교할 JSON을 추가하세요. 이 셀은 시뮬레이션을 시작하지 않습니다.',status='비교 실행 필요')
        common=['mode','years','warmup_minutes','input_hash']
        if code!='X03':
            common+=['model_hash']
        inconsistent=[c for c in common if f[c].isna().any() or f[c].nunique(dropna=False)!=1]
        if code=='X02':
            inconsistent += [c for c in ['load','release_wspw'] if f[c].nunique(dropna=False)!=1]
            if f.seed.nunique()<2:
                inconsistent.append('distinct seed')
        if inconsistent:
            return Analysis({'run_manifest':f},note='조건이 달라 유효한 비교 곡선을 그리지 않습니다: '+', '.join(inconsistent),status='비교 조건 불일치')
        note='완료 lot·관측 구간 정의가 같은지 확인하세요. '
        if code=='X03':
            note+='서로 다른 모델/정책 비교이며 바뀐 조건은 직접 확인해야 합니다.'
        if code=='X02':
            t=f[['completion_wspw','mean_wip','ff_p50','ff_p90','on_time_pct']].agg(['count','mean','std','min','max']).T.reset_index(names='metric')
            return Analysis({'seed_variability':t,'run_manifest':f,'product_type_ff':type_table},y='std',note=note+' 반복 수가 적으면 불확실성 평가도 제한됩니다.')
        return Analysis({'run_comparison':f.sort_values('release_wspw'),'product_type_ff':type_table},'line' if code=='X01' else 'bar',
                        x='release_wspw' if code=='X01' else None,y='ff_p90',note=note)

    @staticmethod
    def labels(frame):
        cols=[c for c in ['product','lot_type','toolgroup','station','step_index','bucket','name','previous','setup','period','file','metric','lot_id'] if c in frame]
        return frame[cols].fillna('N/A').astype(str).agg(' | '.join,axis=1) if cols else frame.index.astype(str)

    def plot(self, result, title=''):
        if result.status in {'비교 실행 필요','비교 조건 불일치','추가 데이터 필요','대용량 계산 선택 필요','표본 없음'}:
            return None
        if not result.tables:
            return None
        if 'priority_service' in result.tables and 'due_z_bands' in result.tables:
            from .priority_analysis import plot_service
            return plot_service(result.tables,product=self.filters['product'])
        frames=[t for t in result.tables.values() if not t.empty]
        f=next((t for t in frames if result.y is None or result.y in t),None)
        if f is None and result.kind=='box':
            f=next((t for t in frames if 'p50' in t),None)
        if f is None:
            return None
        fig,ax=plt.subplots(figsize=(12,5))
        try:
            if result.kind=='box' and {'p5','p25','p50','p75','p95'}.issubset(f):
                t=f if len(f)<=40 else f.head(self.top)
                fig.set_size_inches(12,max(5,len(t)*.3))
                stats=[dict(label=label,whislo=r.p5,q1=r.p25,med=r.p50,q3=r.p75,whishi=r.p95,mean=r['mean'],fliers=[])
                       for label,(_,r) in zip(self.labels(t)+' | n='+t.n.astype(str),t.iterrows())]
                ax.bxp(stats,showmeans=True,orientation='horizontal')
                ax.set_xlabel('Value (see table name for unit); whiskers P5/P95')
            elif result.kind=='scatter' and result.x in f and result.y in f:
                sizes=20+180*f.mean_wip_lots/f.mean_wip_lots.max() if 'mean_wip_lots' in f and f.mean_wip_lots.max()>0 else 20
                color=f[result.hue] if result.hue in f and pd.api.types.is_numeric_dtype(f[result.hue]) else None
                points=ax.scatter(f[result.x],f[result.y],alpha=.45,s=sizes,c=color,cmap='viridis' if color is not None else None)
                if color is not None:
                    fig.colorbar(points,ax=ax,label=result.hue)
                ax.set(xlabel=result.x,ylabel=result.y)
            elif result.kind=='heat' and all(c in f for c in [result.x,result.y,result.hue]):
                t=f.pivot_table(index=result.y,columns=result.x,values=result.hue,aggfunc='sum')
                # Limit rows/columns for readable figures; full table is retained.
                order=t.sum(axis=1).sort_values(ascending=False).head(self.top).index
                t=t.loc[order]
                if result.x not in {'day','step_index'} and len(t.columns)>self.top:
                    t=t.loc[:,t.sum(axis=0).nlargest(self.top).index]
                mesh=ax.imshow(np.ma.masked_invalid(t.to_numpy(float)),aspect='auto',cmap='YlOrRd')
                fig.colorbar(mesh,ax=ax,label=result.hue)
                ticks=np.unique(np.linspace(0,len(t.columns)-1,min(len(t.columns),30),dtype=int))
                ax.set_xticks(ticks,t.columns[ticks].astype(str),rotation=90,fontsize=7)
                ax.set_yticks(range(len(t.index)),t.index.astype(str),fontsize=8)
                ax.set(xlabel=result.x,ylabel=result.y)
            elif result.kind=='stack':
                values=[c for c in BUCKETS if c in f]
                if not values:
                    values=[c for c in ['quantity_short_lot_h','quantity_ready_lot_h','wait_gate','wait_dedicated'] if c in f]
                t=f.head(self.top)
                if not values:
                    plt.close(fig);return None
                t[values].set_axis(self.labels(t)).plot.barh(stacked=True,ax=ax,colormap='tab20')
                ax.legend(fontsize=7,ncol=3)
                ax.set_xlabel('Hours; see table name for lot-hours vs per-lot hours')
            elif result.kind=='timeline':
                colors={k:plt.get_cmap('tab10')(i%10) for i,k in enumerate(f.bucket.unique())}
                for i,(st,g) in enumerate(f.groupby('station')):
                    for _,r in g.iterrows():
                        ax.broken_barh([(r.start_min/1440,(r.end_min-r.start_min)/1440)],(i-.35,.7),facecolors=colors[r.bucket])
                ax.set_yticks(range(f.station.nunique()),sorted(f.station.unique()))
                from matplotlib.patches import Patch
                ax.legend(handles=[Patch(color=v,label=k) for k,v in colors.items()],fontsize=7)
                ax.set_xlabel('Simulation day')
            elif result.kind=='line' and result.x in f and result.y in f:
                groups=[c for c in ['product','lot_type','toolgroup','period'] if c in f and c!=result.x]
                if groups:
                    ranked=f.groupby(groups)[result.y].sum().sort_values(ascending=False).head(self.top).index
                    for key in ranked:
                        key=key if isinstance(key,tuple) else (key,)
                        mask=np.ones(len(f),dtype=bool)
                        for c,k in zip(groups,key):
                            mask &= f[c].eq(k)
                        g=f.loc[mask].sort_values(result.x)
                        ax.plot(g[result.x],g[result.y],label=' | '.join(map(str,key)))
                    ax.legend(fontsize=7,ncol=2)
                else:
                    g=f.sort_values(result.x);ax.plot(g[result.x],g[result.y],marker='o')
                ax.set(xlabel=result.x,ylabel=result.y)
            else:
                numeric=f.select_dtypes(include='number').columns
                y=result.y if result.y in f else ('mean' if 'mean' in f else next((c for c in numeric if c not in {'step_index','n'}),None))
                if y is None:
                    plt.close(fig);return None
                t=f.sort_values(y,ascending=False).head(self.top).iloc[::-1]
                ax.barh(self.labels(t),t[y]);ax.set_xlabel(y)
            ax.set_title(title or 'KPI analysis')
            ax.grid(axis='x',alpha=.2)
            fig.tight_layout()
            return fig
        except Exception:
            plt.close(fig)
            raise

    def analyze_group(self,codes,*,exact_percentiles=False):
        """Related views share tables; richer tables retain every original row."""
        codes=(codes,) if isinstance(codes,str) else tuple(codes)
        if not codes or len(codes)!=len(set(codes)):
            raise ValueError('분석 코드를 중복 없이 하나 이상 지정하세요.')
        results={}
        for code in codes:
            result=self.analyze(code,exact_percentiles=exact_percentiles)
            results[code]=result
            if code in {'O01','O02'} and 'time_flow' in result.tables:
                self.cache[('operation_time_flow',self.start,self.end,self.bin)]=result.tables['time_flow']
        entries=[(code,name,frame) for code,result in results.items() for name,frame in result.tables.items()]
        # Examine the most informative table first, so a basic projection is not
        # displayed before the table that includes it.
        entries.sort(key=lambda item:(-len(item[2].columns),-len(item[2])))
        tables={};aliases={};families={}
        for code,name,frame in entries:
            family=TABLE_FAMILIES.get(name,name)
            target=next((key for key,other in tables.items()
                         if families[key]==family and _table_covers(other,frame)),None)
            if target is None:
                target=code+'_'+name;tables[target]=frame;families[target]=family
            aliases[(code,name)]=target
        # Display in the original analysis order while retaining the richest table.
        order=list(dict.fromkeys(aliases[(code,name)] for code,result in results.items() for name in result.tables))
        return AnalysisGroup(results,{key:tables[key] for key in order},aliases)

    def show(self, code, *, save=False, exact_percentiles=False):
        return self.show_group([code],save=save,exact_percentiles=exact_percentiles)

    def show_group(self,codes,*,save=False,exact_percentiles=False):
        from IPython.display import display, Markdown
        group=self.analyze_group(codes,exact_percentiles=exact_percentiles)
        if save:
            if self.output is None:
                raise ValueError('저장하려면 Explorer output 폴더를 지정하세요.')
            self.output.mkdir(parents=True,exist_ok=True)
        seen_plots=set();figures=[]
        for code,r in group.results.items():
            display(Markdown(f'**{code} — {r.status}**\n\n{r.note}'))
            views=[(r,code,code)]
            for name,table in list(r.tables.items())[1:]:
                if name in {'ff_raw','ff_paper','positive_excess_h'}:
                    extra=Analysis({name:table},'box')
                elif name=='gantt':extra=Analysis({name:table},'timeline')
                else:continue
                views.append((extra,code+' | '+name,group.aliases[(code,name)]))
            for view,title,filename in views:
                keys=tuple(group.aliases[(code,name)] for name in view.tables)
                signature=(view.kind,view.x,view.y,view.hue,keys)
                if signature in seen_plots:continue
                fig=self.plot(view,title)
                if fig is None:continue
                seen_plots.add(signature);display(fig)
                try:
                    if save:fig.savefig(self.output/(filename+'.png'),dpi=140,bbox_inches='tight')
                finally:plt.close(fig)
                figures.append(filename+'.png')
        for key,table in group.tables.items():
            sources=['/'.join(source) for source,target in group.aliases.items() if target==key]
            display(Markdown(f'**{key}** — {len(table):,}행; 화면은 처음 {self.top}행 · 공통 표: '+', '.join(sources)))
            display(table.head(self.top))
            if save:table.to_csv(self.output/(key+'.csv'),index=False,encoding='utf-8-sig')
        if save and len(group.results)>1:
            manifest=dict(snapshot=str(self.path),start_min=self.start,end_min=self.end,
                figures=figures,tables=list(group.tables),
                aliases=[dict(code=c,table=n,shared_table=key) for (c,n),key in group.aliases.items()])
            (self.output/('_'.join(group.results)+'_sources.json')).write_text(
                json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
        return None
