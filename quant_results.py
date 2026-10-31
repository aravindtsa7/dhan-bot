"""Inspectable chart coordinates and ledger presentation, independent of Streamlit."""
from html import escape
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

SIDE_LABELS = {'CE':'BUY CE', 'PE':'BUY PE', 'LONG':'LONG FUT', 'SHORT':'SHORT FUT'}
from quant_book import SIDE_LABELS as BOOK_SIDE_LABELS
SIDE_LABELS.update(BOOK_SIDE_LABELS)
SIDE_LABELS['EXPIRY_DAY']='EXPIRY DAY (CE+PE)'
REASONS = {'target':'Target', 'stop_loss':'Stop Loss', 'trailing_stop':'Trailing SL', 'square_off':'EOD',
           'daily_loss_limit':'Daily loss limit', 'daily_profit_target':'Daily profit target'}


def trade_ledger(trades, config):
    if trades.empty: return pd.DataFrame()
    from quant_backtester import Config, quantity
    c = Config(**config)
    f = trades.sort_values(['exit_time','entry_time','trade_id']).reset_index(drop=True)
    labels = f.side.map(SIDE_LABELS).fillna(f.side)
    fees = f.total_cost + f.get('slippage_cost', pd.Series(0.,index=f.index))
    ledger = pd.DataFrame({
        'Trade #':range(1,len(f)+1), 'Date':f.date, 'Side':labels,
        'Contract / Strike':f.contract,
        'Entry basis':f.get('entry_value_type',pd.Series('premium / futures',index=f.index)).fillna('premium / futures'),
        'Entry Time':pd.to_datetime(f.entry_time).dt.strftime('%H:%M:%S'), 'Entry ₹':f.entry_price,
        'Exit Time':pd.to_datetime(f.exit_time).dt.strftime('%H:%M:%S'), 'Exit ₹':f.exit_price,
        'Qty (Lots)':[f'{r.quantity:g} ({r.quantity/(quantity(c,r.date)/c.lots):g})' for r in f.itertuples()],
        'Exit Reason':f.exit_reason.map(REASONS).fillna(f.exit_reason),
        'Gross P&L':f.get('gross_before_slippage',f.gross_pnl), 'Costs':fees,
        'Net P&L (₹)':f.net_pnl, 'Return %':f.roi_pct,
        'Balance (₹)':c.capital+f.net_pnl.cumsum(),
        'Modeled wing fallback':f.get('modeled_wing_fallback',pd.Series(False,index=f.index)).fillna(False),
    })
    if c.strategy=='expiry_day':
        ledger['Signal Date & Time']=pd.to_datetime(f.signal_time).dt.strftime('%Y-%m-%d %H:%M:%S')
        ledger['Entry Date & Time']=pd.to_datetime(f.entry_time).dt.strftime('%Y-%m-%d %H:%M:%S')
        ledger['Exit Date & Time']=pd.to_datetime(f.exit_time).dt.strftime('%Y-%m-%d %H:%M:%S')
        ledger['CE Entry → Exit ₹']=[f'{r.ce_entry_price:,.2f} → {r.ce_exit_price:,.2f}' for r in f.itertuples()]
        ledger['PE Entry → Exit ₹']=[f'{r.pe_entry_price:,.2f} → {r.pe_exit_price:,.2f}' for r in f.itertuples()]
        ledger['Combined Entry ₹ → Combined Exit ₹']=[f'{r.combined_entry_value:,.2f} → {r.combined_exit_value:,.2f}' for r in f.itertuples()]
        ledger['Combined Return %']=f.combined_return_pct
    return ledger


def ledger_html(ledger):
    """Side badges and signed P&L; escape all user/contract text."""
    rows = []
    for record in ledger.to_dict('records'):
        cells = []
        for key, value in record.items():
            cls = ''
            if key == 'Side':
                cls = 'trade-badge buy' if value in ('BUY CE','LONG FUT') else 'trade-badge sell'
            elif key in ('Net P&L (₹)', 'Gross P&L', 'Return %'):
                cls = 'positive' if value>=0 else 'negative'
            display = f'{value:,.2f}' if isinstance(value,float) else str(value)
            cells.append(f'<td><span class="{cls}">{escape(display)}</span></td>')
        rows.append('<tr>'+''.join(cells)+'</tr>')
    headings = ''.join(f'<th>{escape(c)}</th>' for c in ledger)
    return '<div class="table-scroll trade-ledger"><table class="terminal-table"><thead><tr>'+headings+'</tr></thead><tbody>'+''.join(rows)+'</tbody></table></div>'


def trade_chart(trade, underlying, paths, config):
    """Underlying session above, fixed execution contract below; never put
    option-premium markers on the underlying's different price scale.
    """
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=.09,
                        row_heights=[.55,.45], subplot_titles=('Underlying session · IST', 'Traded contract · IST'))
    if not underlying.empty:
        if config['strategy']=='expiry_day' and 'date' in underlying:
            f=underlying[underlying.date.between(str(pd.Timestamp(trade['entry_time']).date()),str(pd.Timestamp(trade['exit_time']).date()))]
        else:
            f = underlying[underlying.date.eq(trade['date'])] if 'date' in underlying else underlying
        fig.add_trace(go.Candlestick(x=f.timestamp,open=f.open,high=f.high,low=f.low,close=f.close,name='Underlying'),row=1,col=1)
        for key,label,color in [('vwap','VWAP / TWAP proxy','#fbbf24'),('ema9','EMA 9','#22d3ee'),('ema21','EMA 21','#a78bfa'),('supertrend','Supertrend','#34d399')]:
            if key in f: fig.add_trace(go.Scatter(x=f.timestamp,y=f[key],name=label,line=dict(color=color,width=1)),row=1,col=1)
        opening = 1020 if config['instrument'] in ('NATURALGAS','CRUDEOIL','GOLDM') else 555
        times = f.timestamp.dt.hour*60+f.timestamp.dt.minute
        n = int(config['timeframe'][:-1])
        end = opening+(30 if opening==1020 else 15)
        orb = f[(times>=opening)&(times+n<=end)]
        if not orb.empty:
            visible = f[times>=end]
            for value,label in [(orb.high.max(),'ORB High'),(orb.low.min(),'ORB Low')]:
                fig.add_trace(go.Scatter(x=visible.timestamp,y=[value]*len(visible),name=label,line=dict(color='#94a3b8',dash='dot')),row=1,col=1)
    trace = paths[paths.trade_id.eq(trade['trade_id'])] if not paths.empty else paths
    if not trace.empty:
        fig.add_trace(go.Candlestick(x=trace.timestamp,open=trace.open,high=trace.high,low=trace.low,close=trace.close,name='Execution candles'),row=2,col=1)
        fig.add_trace(go.Scatter(x=trace.timestamp,y=trace.stop,name='Active stop',line=dict(color='#fb7185',shape='hv')),row=2,col=1)
        if 'price_source' in trace:
            modeled = trace[~trace.price_source.isin(['observed','observed_price_volume_corrected'])]
            fig.add_trace(go.Scatter(x=modeled.timestamp,y=modeled.close,mode='markers',name='Modeled candle',marker=dict(color='#fbbf24',symbol='diamond',size=6)),row=2,col=1)
    side = SIDE_LABELS.get(trade['side'],trade['side'])
    contract = f"{trade['strike']:g} {trade['side']}" if trade['side'] in ('CE','PE') else trade['contract']
    entry_at, exit_at = pd.Timestamp(trade['entry_time']), pd.Timestamp(trade['exit_time'])
    entry = f"{side.split()[0]} {contract} @ ₹{trade['entry_price']:,.2f} ({entry_at:%H:%M})"
    reason = REASONS.get(trade['exit_reason'],trade['exit_reason'])
    pnl = ('+' if trade['net_pnl']>=0 else '−')+f"₹{abs(trade['net_pnl']):,.2f}"
    exit_text = f"EXIT @ ₹{trade['exit_price']:,.2f} ({pnl} | {'Target Hit' if reason=='Target' else reason})"
    for at,price,name,symbol,color,text in [
        (entry_at,trade['entry_price'],'▲ ENTRY','triangle-up','#10b981',entry),
        (exit_at,trade['exit_price'],'▼ EXIT','triangle-down','#22d3ee',exit_text)]:
        fig.add_trace(go.Scatter(x=[at],y=[price],name=name,mode='markers',marker=dict(symbol=symbol,color=color,size=15),text=[text],hovertemplate='%{text}<extra></extra>'),row=2,col=1)
        fig.add_annotation(x=at,y=price,text=escape(text),showarrow=True,arrowhead=2,
                           ax=0,ay=-45 if name=='▲ ENTRY' else 45,font=dict(color=color,size=10),row=2,col=1)
    for key,label,color in [('entry_price','Entry price','#94a3b8'),('initial_stop','Stop loss price','#ef4444'),('target','Target price','#22d3ee')]:
        if key=='target' and config.get('exit_mode')=='end_of_day': continue
        fig.add_trace(go.Scatter(x=[entry_at,exit_at],y=[trade[key]]*2,name=label,mode='lines',line=dict(color=color,dash='dash',width=1)),row=2,col=1)
    fig.update_xaxes(rangeslider_visible=False)
    fig.update_layout(title=side+' · '+str(trade.get('entry_value_type','premium / futures'))+' · '+contract)
    if config['strategy']=='expiry_day':
        summary=(f"Signal {pd.Timestamp(trade['signal_time']):%Y-%m-%d %H:%M} | Fill {entry_at:%Y-%m-%d %H:%M} → Exit {exit_at:%Y-%m-%d %H:%M} IST<br>"
                 f"CE ₹{trade['ce_entry_price']:.2f} → ₹{trade['ce_exit_price']:.2f} | "
                 f"PE ₹{trade['pe_entry_price']:.2f} → ₹{trade['pe_exit_price']:.2f} | "
                 f"Combined ₹{trade['combined_entry_value']:,.2f} → ₹{trade['combined_exit_value']:,.2f} ({trade['combined_return_pct']:+.2f}%)")
        fig.add_annotation(x=0,y=1.08,xref='paper',yref='paper',text=summary,showarrow=False,xanchor='left',font=dict(size=10))
    fig.update_yaxes(tickprefix='₹')
    return fig
