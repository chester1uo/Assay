"""Bounded experiment views for agent context; full evidence stays in SQLite."""
from __future__ import annotations


_METRICS = ('failure_mode','suggestion','rank_ic','rank_icir','ic','icir','turnover_1d',
    'decay_halflife_days','ic_by_horizon','n_dates','n_symbols','eval_period','panel_period',
    'score_period','scoring_complete','universe_id','universe_as_of','universe_symbols_hash',
    'universe_axis','data_as_of','execution','adj','panel_content_hash','lookahead_detected',
    'diagnostics_scope','unavailable_score_metrics')
_PORTFOLIO = ('status','total_return','annual_return','gross_return','sharpe','max_drawdown',
    'annual_turnover','cost_drag','spec_hash','panel_content_hash','cost_assumptions','warnings',
    'n_trading_days','n_rebalances','period_start','period_end')


def bounded(value, depth=3):
    """Bound nested diagnostic metadata as well as the number of returned rows."""
    if isinstance(value,str):
        return value if len(value)<=1200 else value[:1200]+'… [truncated]'
    if depth<=0 and isinstance(value,(dict,list)):
        return {'detail_omitted':True,'total':len(value)}
    if isinstance(value,dict):
        keys=list(value)[:24]
        result={k:bounded(value[k],depth-1) for k in keys}
        if len(keys)<len(value):
            result['__truncated_fields__']=len(value)-len(keys)
        return result
    if isinstance(value,list):
        if len(value)<=8:
            return [bounded(v,depth-1) for v in value]
        return {'items':[bounded(v,depth-1) for v in value[:8]],'total':len(value),'truncated':True}
    return value


def summarize_attempt(attempt):
    result={k:bounded(v) for k,v in attempt.items() if k not in {'report','panel_report'}}
    report=attempt.get('report')
    if isinstance(report,dict):
        result['report']={k:bounded(report[k]) for k in _METRICS if k in report}
        diag=report.get('diagnostics')
        if isinstance(diag,dict):
            result['report']['diagnostics']={k:bounded(diag[k]) for k in
                ('ok','status','stage_reached','failure_mode','lookahead_detected','nan_fraction','coverage','suggestion','stats') if k in diag}
            for key in ('errors','warnings'):
                if key in diag:
                    result['report']['diagnostics'][key]=bounded([
                        {k:row[k] for k in ('code','severity','message','suggestion','stage') if k in row}
                        if isinstance(row,dict) else row for row in diag[key]])
        pf=report.get('portfolio')
        if isinstance(pf,dict):
            result['report']['portfolio']={k:bounded(pf[k]) for k in _PORTFOLIO if k in pf}
    else:
        result['report']=report
    result['report_detail']='summary; inspect trades, positions, nav, ic or diagnostics with offset/limit'
    return result


def inspect_experiment(attempt, *, section='summary', offset=0, limit=20):
    if type(offset) is not int or offset<0 or type(limit) is not int or not 1<=limit<=100:
        raise ValueError('require offset >= 0 and 1 <= limit <= 100')
    if section=='summary':
        return summarize_attempt(attempt)
    report=attempt.get('report') or {}
    pf=report.get('portfolio') or {}
    if section in {'trades','positions'}:
        rows=pf.get('trade_log' if section=='trades' else 'position_log') or []
    elif section=='nav':
        dates,values=pf.get('nav_dates') or [],pf.get('nav_series') or []
        if len(dates)!=len(values):
            raise ValueError('saved NAV dates and values are misaligned')
        rows=[dict(date=d,nav=v) for d,v in zip(dates,values)]
    elif section=='ic':
        dates,values=report.get('dates') or [],report.get('ic_series') or []
        rank=report.get('rank_ic_series') or []
        if len(dates)!=len(values) or len(dates)!=len(rank):
            raise ValueError('saved IC series are misaligned')
        rows=[dict(date=d,ic=v,rank_ic=r) for d,v,r in zip(dates,values,rank)]
    elif section=='diagnostics':
        rows=[]
        for key,value in (report.get('diagnostics') or {}).items():
            if isinstance(value,list):
                rows.extend(dict(key=key,index=i,value=bounded(v)) for i,v in enumerate(value))
            else:
                rows.append(dict(key=key,value=bounded(value)))
    else:
        raise ValueError('section must be summary, trades, positions, nav, ic or diagnostics')
    end=min(offset+limit,len(rows))
    return dict(id=attempt['id'],status=attempt['status'],section=section,offset=offset,limit=limit,
                total=len(rows),items=rows[offset:end],has_more=end<len(rows),
                next_offset=end if end<len(rows) else None)


def inspect_factor_history(history, *, offset=0, limit=20):
    """Page both chronological history lists without embedding full backtests."""
    if type(offset) is not int or offset<0 or type(limit) is not int or not 1<=limit<=100:
        raise ValueError('require offset >= 0 and 1 <= limit <= 100')
    result={k:bounded(v) for k,v in history.items() if k not in {'experiments','decisions'}}
    experiments,decisions=history.get('experiments',[]),history.get('decisions',[])
    result['experiments']=[summarize_attempt(a) for a in experiments[offset:offset+limit]]
    result['decisions']=[bounded(d) for d in decisions[offset:offset+limit]]
    total=max(len(experiments),len(decisions))
    result['history_page']=dict(offset=offset,limit=limit,experiment_total=len(experiments),
        decision_total=len(decisions),has_more=offset+limit<total,
        next_offset=offset+limit if offset+limit<total else None,order='oldest_first')
    return result
