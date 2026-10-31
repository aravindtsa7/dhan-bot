"""Minute-synchronized account orchestration; no future exit proceeds fund entries."""
from collections import defaultdict


def execute_portfolio(data,c,run_id,daily_audit=None):
    from quant_backtester import _trade_steps, HIGH_STRATEGIES, LEGACY_STRATEGIES as STRATEGIES, RESEARCH_STRATEGIES, BOOK_STRATEGIES
    records,paths,skipped=[],[],[]
    equity=c.capital
    serial=0
    for date in sorted(data.days):
        start_equity=equity
        positive=lambda *values: min((x for x in values if x>0),default=float('inf'))
        profit=positive(c.daily_profit_target,start_equity*c.daily_profit_pct/100)
        loss=positive(c.daily_loss_limit,start_equity*c.daily_loss_pct/100)
        audit=dict(date=date,start_equity=start_equity,net_pnl=0.,trades=0,peak_net_pnl=0.,minimum_net_pnl=0.,
            max_drawdown_1m_close=0.,lock_reason='',lock_minute=None,lock_trigger_pnl=None,
            profit_limit=profit if profit<float('inf') else None,loss_limit=loss if loss<float('inf') else None,
            blocked_entries=0,loss_limit_excess=0.,eligible=True)
        events=defaultdict(list)
        for event in data.events(c,date): events[event[0]].append(event)
        if not events:
            if daily_audit is not None: daily_audit.append(audit)
            continue
        active=[]; entered=[]; day_closed=[]; locked=None; last_exit=-1
        cap=1 if c.strategy in (HIGH_STRATEGIES[2],HIGH_STRATEGIES[3]) else c.max_trades_day
        pair=c.strategy==STRATEGIES[2] and c.mode=='B'
        finish=1395 if data.mcx else 920
        last_event=max(events)
        for at in range(min(events),finish+1):
            # Entries happen at the open. Positions carried into this minute
            # still reserve cash, even if a later intrabar stop will close them.
            for event in events.get(at,[]):
                if locked:
                    audit['blocked_entries']+=1
                    continue
                if len(entered)>=cap or equity<=0: continue
                if not data.mcx and 690<=at<795 and c.strategy not in RESEARCH_STRATEGIES+BOOK_STRATEGIES and c.strategy!=HIGH_STRATEGIES[4]: continue
                if not pair and (active or at<=last_exit): continue
                if c.strategy in (HIGH_STRATEGIES[1],STRATEGIES[2]) and event[1] in entered: continue
                if c.strategy==STRATEGIES[2] and not pair and day_closed and day_closed[-1]['exit_reason'] not in ('stop_loss','trailing_stop'): continue
                reserved=sum(p['reserved_capital'] for p in active)
                unrealized=sum(p['net_mtm'] for p in active)
                # Do not borrow unrealized gains; unrealized losses reduce buying power.
                available_equity=max(0,equity+min(0,unrealized))
                base=available_equity if c.compound else min(c.capital,available_equity)
                ceiling=base*c.capital_usage_pct/100
                budget=max(0,min(ceiling-reserved,available_equity-reserved))
                if pair: budget=min(budget,ceiling/2)
                generator=_trade_steps(data,c,date,event,f'{run_id}_{serial:06d}',budget=budget,entry_equity=available_equity)
                serial+=1
                active.append(dict(generator=generator,reserved_capital=0,net_mtm=0,new=True,side=event[1],event=event))
                # Prime immediately so a second leg sees the first reservation.
                p=active[-1]
                try:
                    p.update(next(generator)); p['new']=False; p['processed_at']=at-1
                    entered.append(event[1])
                except StopIteration as done:
                    trade,trace=done.value; active.remove(p)
                    if trade is None:
                        reason='unavailable_entry_or_insufficient_capital'
                        first=None if c.strategy in BOOK_STRATEGIES[:3] and not data.mcx else data.price(data.days[date],at,event[1],event[2])
                        if not data.mcx:
                            if c.option_side not in ('both',event[1]): reason='option_side_filter'
                            elif first and max(first[0],event[3] or first[0])<c.minimum_premium: reason='below_minimum_premium'
                        skipped.append(dict(date=date,minute=at,side=event[1],budget=budget,reason=reason))
                    else:
                        entered.append(event[1]); records.append(trade); paths.extend(trace); day_closed.append(trade)
                        equity+=trade['net_pnl']; last_exit=at
                    # Even same-minute exits cannot finance another entry at
                    # this same open. Dual legs reserve independently below.
                    if pair: break
            if pair and events.get(at):
                new_legs=[p for p in active if p['event'][0]==at]
                if len(new_legs)!=2:
                    # Both anchor orders must be affordable before either is
                    # executed. Priming reserves capital but reads no bar path.
                    for p in new_legs:
                        p['generator'].close(); active.remove(p); entered.remove(p['side'])
                    if new_legs:
                        skipped.append(dict(date=date,minute=at,side='CE+PE',budget=ceiling,reason='straddle_pair_unaffordable_or_missing'))
            for p in list(active):
                if p.get('processed_at')==at: continue
                try: p.update(p['generator'].send(locked)); p['processed_at']=at
                except StopIteration as done:
                    trade,trace=done.value
                    if trade is None: raise AssertionError('Open position disappeared')
                    records.append(trade); paths.extend(trace); day_closed.append(trade)
                    equity+=trade['net_pnl']; last_exit=at; active.remove(p)
            day_net=equity-start_equity+sum(p['net_mtm'] for p in active)
            audit['peak_net_pnl']=max(audit['peak_net_pnl'],day_net)
            audit['minimum_net_pnl']=min(audit['minimum_net_pnl'],day_net)
            audit['max_drawdown_1m_close']=max(audit['max_drawdown_1m_close'],audit['peak_net_pnl']-day_net)
            if not locked:
                if day_net>=profit-1e-8: locked='daily_profit_target'
                elif day_net<=-loss+1e-8: locked='daily_loss_limit'
                if locked:
                    audit.update(lock_reason=locked,lock_minute=at,lock_trigger_pnl=day_net)
            # Once flat and no future signal remains, later constant marks
            # cannot change risk statistics or the latched daily lock.
            if not active and at>=last_event: break
        if active: raise AssertionError('Unresolved positions at square-off')
        audit.update(net_pnl=equity-start_equity,trades=len(day_closed),
            loss_limit_excess=max(0.,start_equity-equity-loss) if loss<float('inf') else 0.)
        if daily_audit is not None: daily_audit.append(audit)
    return records,paths,skipped
