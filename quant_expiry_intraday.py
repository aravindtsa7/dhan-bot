"""Standalone expiry-morning strategy; no BTST scheduling or overnight hold."""
from dataclasses import replace
from datetime import datetime
from quant_calendar import is_expiry_day, regular_session

LABEL='EXPIRY INTRADAY (CE+PE)'


def intraday_events(data,c,date):
    from quant_backtester import minute
    if not regular_session(date) or not is_expiry_day(c.instrument,date):return []
    return [(minute(c.intraday_entry_signal)+1,'EXPIRY_INTRADAY',0,None,0)]


def live_event(c,now):
    from paper_feed import IST
    local=datetime.fromtimestamp(now,IST)
    events=intraday_events(None,c,local.date().isoformat())
    return events[0] if events and local.hour*60+local.minute==events[0][0] and local.second<=15 else None


def intraday_steps(data,c,date,event,trade_id,budget=None,entry_equity=None):
    # Reuse the atomic fixed-basket execution primitive unchanged. Scheduling,
    # catalog identity, timing and presentation belong to this separate strategy.
    from quant_expiry import expiry_steps
    if event not in intraday_events(data,c,date):return None,[]
    execution=replace(c,expiry_exit_cutoff=c.intraday_exit_cutoff,
                      exit_mode='target_eod' if c.exit_mode=='legacy' else c.exit_mode)
    trade,path=yield from expiry_steps(data,execution,date,event,trade_id,budget,entry_equity,exit_date=date)
    if trade:
        trade.update(side='EXPIRY_INTRADAY',structure_label=LABEL,expiry_session_basis='calendar',
                     retrospective_expiry_inference=False,overnight=False)
        if trade['verification_status']=='rolling_btst_expiry_unverified':
            trade['verification_status']='rolling_intraday_expiry_unverified'
    return trade,path
