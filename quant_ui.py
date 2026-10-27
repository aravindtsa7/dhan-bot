r"""Local research UI. Run: .venv\Scripts\python -m streamlit run quant_ui.py"""
from dataclasses import replace
from datetime import date, timedelta
import json
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
from plotly.subplots import make_subplots
import streamlit as st

from quant_lake import QuantDataLake, ROOT, IST
from quant_paper_ui import render_live_paper_room
from quant_option_chain_ui import render_option_chain
from quant_backtester import Config, OUT, STRATEGIES, HIGH_STRATEGIES, INSTRUMENTS, POINT_VALUE, LIMITATIONS, run_backtest, refresh_leaderboard, make_bars

st.set_page_config(page_title='Quant Research | Dhan Lake',page_icon='◈',layout='wide')
st.markdown('''<style>
 .stApp {background:#0b101a;color:#d9e2f1} .block-container{padding-top:2rem}
 div[data-testid="stMetric"]{background:#141e2e;border:1px solid #24344b;border-radius:10px;padding:14px}
 h1,h2,h3{letter-spacing:-.025em} .stTabs [data-baseweb="tab-list"]{gap:22px}
 </style>''',unsafe_allow_html=True)
st.title('◈ Quant Research')
st.caption('OPTIONS & COMMODITIES  /  HISTORICAL RESEARCH  /  IST')
st.warning('Zero unresolved positions is an accounting check, not price verification. Option continuity estimates are tagged as modeled; rolling expiry IDs remain unverified. Use the verified-price filter to exclude these runs.')
with st.expander('Methodology & data limits'):
    for note in LIMITATIONS: st.write('• '+note)
    st.write('H1–H6 use completed observed candles and next-minute entries. Option VWAP is calculated per fixed strike; H1 requires complete session-to-signal coverage. Volume confirmation uses the last completed minute versus its preceding 20 minutes, with a five-minute OI change. No index entries from 11:30–13:15 except H5 expiry afternoon; H3/H4 permit one entry per session. Missing option candles use tagged estimates only for managing an already-open position. Stops win ambiguous collisions; trailing and breakeven changes apply next minute.')


@st.cache_data(ttl=10)
def leaderboard(source='leaderboard.csv'):
    p=OUT/source
    return pd.read_csv(p) if p.exists() else pd.DataFrame()


@st.cache_data(ttl=60)
def table(run_id,name):
    path=OUT/'runs'/run_id/f'{name}.parquet'
    return pd.read_parquet(path) if path.exists() else pd.DataFrame()


@st.cache_data(ttl=60)
def result(run_id):
    return json.loads((OUT/'runs'/run_id/'result.json').read_text())


@st.cache_data(ttl=120)
def query(instrument,start,end,dataset='spot_1m'):
    with QuantDataLake() as lake: return lake.query(instrument,start,end,dataset=dataset)


def theme(fig,height=420):
    fig.update_layout(template='plotly_dark',paper_bgcolor='#0b101a',plot_bgcolor='#101827',height=height,
                      margin=dict(l=25,r=25,t=35,b=25),font=dict(color='#c6d3e5'),legend=dict(orientation='h'))
    return fig


def candles(fig,frame,row,name):
    fig.add_trace(go.Candlestick(x=frame.timestamp,open=frame.open,high=frame.high,low=frame.low,close=frame.close,
                                name=name,increasing_line_color='#36cfaf',decreasing_line_color='#ff6685'),row=row,col=1)


NAMES={'NIFTY':'Nifty 50','SENSEX':'Sensex','CRUDEOIL':'Crude Oil','NATURALGAS':'Natural Gas','GOLDM':'Gold Mini'}


def trade_ledger(trades):
    """Old runs retain missing allocation fields as N/A, never reconstructed guesses."""
    columns={'trade_id':'Trade ID','instrument':'Instrument','strategy':'Strategy Name',
             'entry_time':'Entry Date/Time','exit_time':'Exit Date/Time','contract':'Strike / Contract',
             'capital_allocated':'Capital Allocated (₹)','capital_used_pct':'Capital Used (%)',
             'entry_price':'Entry Price','exit_price':'Exit Price','net_pnl':'Net P&L (₹)',
             'roi_pct':'ROI (%)','exit_reason':'Exit Reason','used_modeled_prices':'Modeled Prices'}
    ledger=trades.reindex(columns=list(columns)).rename(columns=columns).copy()
    ledger['Instrument']=ledger['Instrument'].map(NAMES)
    ledger['Exit Reason']=ledger['Exit Reason'].replace({'target':'Target','stop_loss':'SL','trailing_stop':'Trailing',
        'square_off':'Square-off','daily_profit_target':'Daily profit goal','daily_loss_limit':'Daily loss limit'})
    return ledger


collection=st.sidebar.selectbox('Result collection',['Current engine']+(['Step 4 archive'] if (OUT/'leaderboard_step4.csv').exists() else []))
lb=leaderboard('leaderboard_step4.csv' if collection=='Step 4 archive' else 'leaderboard.csv')
with st.sidebar:
    st.header('Research universe')
    instrument=st.selectbox('Instrument',['All']+INSTRUMENTS,format_func=lambda v:NAMES.get(v,v))
    begin=st.date_input('From Date',date(2025,1,1),min_value=date(2025,1,1),max_value=date(2026,10,22))
    end=st.date_input('To Date (inclusive)',date(2026,10,22),min_value=date(2025,1,1),max_value=date(2026,10,22))
    strategy_mode=st.selectbox('Strategy mode',['Steady Income','High-Risk Explosive Growth'])
    growth=strategy_mode=='High-Risk Explosive Growth'
    capital=st.number_input('Total trading capital (₹)',min_value=1000,value=100000,step=10000)
    usage=st.slider('Capital usage / maximum concurrent exposure (%)',1,100,75,1)
    sizing=st.selectbox('Position sizing',['Allocate whole lots','Fixed lots'])
    compound=st.checkbox('Reinvest realized profits',value=growth)
    if growth:
        daily_profit_pct=st.slider('Daily profit goal (% of opening equity)',10,200,10)
        daily_target=0
    else:
        daily_target=st.number_input('Daily profit goal (₹; 0 disables)',min_value=0,value=1500,step=500)
        daily_profit_pct=0
    daily_loss=st.number_input('Daily loss limit (₹; 0 disables)',min_value=0,value=2000,step=500)
    daily_loss_pct=st.slider('Daily loss limit (%; 0 disables)',0,100,0)
    st.caption('Controls configure the next custom run. Goals are exit triggers, not promised returns. Limits use net realized + open P&L and exit next minute; gaps can overshoot.')
    st.divider()
    family=st.selectbox('Strategy family',['All']+STRATEGIES)
    tf=st.selectbox('Timeframe',['All','3m','5m','15m'])
    strike=st.selectbox('Strike',['All','ATM','ATM-1','ATM+1'])
    min_trades=st.number_input('Minimum closed trades',min_value=0,value=1,step=10)
    verification=st.selectbox('Price evidence',['All research (models included)','Observed contract prices + accounting checked'])
    if st.button('Refresh results'):
        st.cache_data.clear(); st.rerun()
    st.caption('Historical research • no broker connection • no order placement')
filtered=lb.copy()
if not filtered.empty:
    filtered=filtered[filtered.total_trades.ge(min_trades)]
    if verification!='All research (models included)':
        filtered=filtered[filtered.get('verified_price_eligible',pd.Series(False,index=filtered.index)).eq(True)]
    for column,value in [('instrument',instrument),('strategy',family),('timeframe',tf),('strike',strike)]:
        if value!='All': filtered=filtered[filtered[column].eq(value)]
    labels={r.run_id:f'{r.instrument} · {r.strategy} · {r.timeframe} · {r.strike} · ₹{r.net_pnl:,.0f} · {r.run_id[:6]}' for r in filtered.itertuples()}
else: labels={}
selected=st.selectbox('Active backtest',list(labels),format_func=lambda v:labels[v]) if labels else None
if selected:
    r=result(selected); m=r['metrics']; c=r['config']
    cols=st.columns(6)
    for col,label,value in zip(cols,['Net after costs','Win rate','Profit factor','Realized max DD','Unresolved / modeled trades','Eligible / excluded sessions'],
                              [f'₹{m["net_pnl"]:,.0f}',f'{m["win_rate"]:.1f}%',('∞' if m.get('profit_factor_infinite') else f'{m["profit_factor"]:.2f}' if m['profit_factor'] is not None else 'N/A'),
                               f'₹{m["max_drawdown"]:,.0f}',f'{m.get("unresolved_trades",m["censored_trades"])} / {m.get("modeled_trades",0)}',f'{r["eligible_sessions"]} / {r["excluded_sessions"]}']):
        col.metric(label,value)
    st.caption(f'Price evidence: {r["status"]} | {m["total_trades"]} closed trades | Sharpe {m["sharpe"]:.2f} | 2025 net ₹{m["train_2025_net"]:,.2f} | 2026 net ₹{m["validation_2026_net"]:,.2f}')
    st.caption(f'Saved engine {r["engine_version"]} | {c["start"]} to {c["end"]} (exclusive) | original capital ₹{c["capital"]:,.0f} | skipped entries {m.get("skipped_entries",0)}. Sidebar changes require a new run; they do not rescale saved metrics.')
    with st.expander('Saved run configuration and allocation skips'):
        st.json(c)
        st.dataframe(table(selected,'skipped_entries'),hide_index=True,width='stretch')
tabs=st.tabs(['🏆 Leaderboard & Optimizer','📈 Performance Analytics','🕯️ Trade Inspector','🗄️ Lake & Health','📊 Live Paper Trading Room','📊 Option Chain & Strike Replay'])

with tabs[0]:
    st.subheader('Strategy discovery')
    st.caption('All entered trades are accounted for, including tagged modeled exits. A verified-price run uses observed fixed-contract candles and reconciled accounting; fee, margin and fill assumptions still apply. Cross-market coverage differs. Full-window grid ranking makes the yearly split descriptive, not untouched validation.')
    if not filtered.empty:
        columns=['run_id','instrument','strategy','timeframe','strike','oi_mode','sl_pct','target_pct','trail_pct','net_pnl','roi_pct','win_rate','profit_factor','max_drawdown','sharpe','calmar','total_trades','unresolved_trades','modeled_trades','status','train_2025_net','validation_2026_net','first_session','last_session']
        st.dataframe(filtered[[col for col in columns if col in filtered]],hide_index=True,width='stretch')
        st.download_button('Export filtered leaderboard',filtered.to_csv(index=False),'leaderboard.csv','text/csv')
    else: st.info('No matching results. Run the research sweep or a custom configuration below.')
    st.subheader('Custom backtest')
    # Strategy selection is outside the form so instrument-dependent controls update immediately.
    custom_instrument=instrument if instrument!='All' else st.selectbox('Test instrument',INSTRUMENTS,key='custom_instrument',format_func=lambda v:NAMES[v])
    choices=([STRATEGIES[4],HIGH_STRATEGIES[3]]+([HIGH_STRATEGIES[5]] if custom_instrument!='GOLDM' else [])) if custom_instrument in POINT_VALUE else HIGH_STRATEGIES[:3]+STRATEGIES[:4]+[HIGH_STRATEGIES[4]]
    custom_strategy=st.selectbox('Test strategy',choices,index=len(choices)-1 if growth else 0)
    st.caption(f'New run: {NAMES[custom_instrument]} | {begin} through {end} | capital ₹{capital:,.0f} | exposure ceiling {usage}% | {strategy_mode}. Saved results below retain their original configuration.')
    with st.form('backtest'):
        a,b,d=st.columns(3)
        with a:
            custom_tf=st.selectbox('Signal timeframe',['3m','5m','15m'],index=1)
            custom_strike=st.selectbox('Moneyness',['ATM','ATM-1','ATM+1'])
            sl=st.slider('Option stop loss (%)',5,60,25,disabled=custom_instrument in POINT_VALUE)
            target=st.slider('Option target (%)',10,300,150 if growth else 50,disabled=custom_instrument in POINT_VALUE)
            if custom_instrument in POINT_VALUE:
                trail=20 if st.checkbox('Enable ATR trailing',value=True) else 0
                jump=5
                atr_jump=st.slider('MCX trailing jump (fraction of initial stop)',0.1,1.0,0.25,0.05)
                st.caption('MCX trailing distance uses the initial ATR stop distance; each jump advances by the configured fraction.')
            else:
                atr_jump=.25
                trail=st.slider('Trailing distance (% of entry; 0 disables)',0,60,20)
                jump=st.slider('Trailing jump (% of entry)',1,25,5)
        with b:
            lots=st.number_input('Fixed lots',min_value=1,max_value=100,value=1,disabled=sizing!='Fixed lots')
            max_lots=st.number_input('Maximum lots for allocation sizing',min_value=1,max_value=1000,value=100)
            margin=st.slider('Futures margin assumption (% of notional)',1,100,100,disabled=custom_instrument not in POINT_VALUE)
            st.caption('Options allocate the premium. Futures use the configured margin assumption; 100% is fully funded notional. Lower values model leverage, not verified historical broker margin. Round-trip fees are reserved separately.')
            units=st.number_input('Lot size / point multiplier override (0 = schedule)',min_value=0,max_value=10000,value=0)
            slip=st.slider('Slippage (price points / fill)',0.0,5.0,1.0 if custom_instrument in POINT_VALUE else 0.5,0.1)
        with d:
            entry_start=st.text_input('Entry window start (IST)','17:30' if custom_instrument in POINT_VALUE else '13:00' if custom_strategy==HIGH_STRATEGIES[4] else '09:30')
            entry_end=st.text_input('Entry window end (IST)','21:30' if custom_instrument in POINT_VALUE else '11:15' if custom_strategy==HIGH_STRATEGIES[2] else '14:30')
            orb=st.selectbox('Opening range minutes',[15,30])
            mode=st.selectbox('Legacy straddle mode',['A','B'],disabled=custom_strategy!=STRATEGIES[2])
            if custom_strategy==STRATEGIES[2]: st.caption('Mode B reserves half the account ceiling for each leg and sets the daily entry cap to two legs.')
            anchor=st.selectbox('Dual-leg anchor (IST)',['09:25','09:30','13:30'],disabled=custom_strategy not in (HIGH_STRATEGIES[1],STRATEGIES[2]))
            breakout=st.slider('Dual-leg breakout (%)',18,40,18,disabled=custom_strategy not in (HIGH_STRATEGIES[1],STRATEGIES[2]))
            atr_sl=st.slider('MCX ATR stop multiplier',0.5,4.0,1.5,0.25,disabled=custom_instrument not in POINT_VALUE)
            rr=st.slider('MCX target / stop distance',1.0,5.0,2.0,0.5,disabled=custom_instrument not in POINT_VALUE)
            oi_mode=st.selectbox('OI confirmation',['either','rising','unwinding'],disabled=custom_instrument in POINT_VALUE)
            volume_multiple=st.slider('Volume surge multiple',1.5,5.0,1.5,0.1)
            adx=st.slider('Minimum ADX',0,50,20)
            atr_expansion=st.slider('ATR expansion multiple',0.5,3.0,1.5 if custom_strategy==HIGH_STRATEGIES[5] else 1.0,0.1)
            compression=st.slider('Compression ratio',0.2,2.0,0.8,0.1)
            lookback=st.slider('Option / H6 breakout lookback bars',1,20,3)
            max_trades=st.selectbox('Maximum daily entries',[1] if custom_strategy in (HIGH_STRATEGIES[2],HIGH_STRATEGIES[3]) else [1,2],index=0)
            price_policy=st.selectbox('Missing fixed-strike policy',['model_continuity','strict_observed'] if custom_instrument not in POINT_VALUE else ['strict_observed'])
            st.caption('Strict observed policy rejects the entire run if a position loses its fixed-strike candle. Model continuity closes it with explicitly unverified estimates.')
        submitted=st.form_submit_button('Run & save backtest',type='primary')
    if submitted:
        try:
            config=Config(instrument=custom_instrument,strategy=custom_strategy,timeframe=custom_tf,
                          strike='ATM' if custom_strategy in (HIGH_STRATEGIES[1],STRATEGIES[2]) or custom_instrument in POINT_VALUE else custom_strike,
                          start=str(begin),end=str(end+timedelta(days=1)),sl_pct=sl,target_pct=target,trail_pct=trail,trail_jump_pct=jump,
                          capital=capital,lots=lots,lot_size=units,slippage=slip,entry_start=entry_start,entry_end=entry_end,
                          capital_usage_pct=usage,sizing_mode='allocation' if sizing=='Allocate whole lots' else 'fixed_lots',max_lots=max_lots,
                          compound=compound,futures_margin_pct=margin,strategy_mode='growth' if growth else 'steady',
                          daily_profit_target=daily_target,daily_loss_limit=daily_loss,daily_profit_pct=daily_profit_pct,daily_loss_pct=daily_loss_pct,
                          adx_min=adx,atr_expansion=atr_expansion,compression_ratio=compression,option_lookback=lookback,
                          orb_minutes=orb,mode=mode,anchor=anchor,breakout_pct=breakout,atr_sl=atr_sl,reward_risk=rr,
                          atr_trail_jump=atr_jump,
                          oi_mode=oi_mode,volume_multiple=volume_multiple,max_trades_day=2 if custom_strategy==STRATEGIES[2] and mode=='B' else max_trades,price_policy=price_policy)
            with st.spinner('Loading eligible sessions and simulating actual candles…'):
                custom=run_backtest(config); refresh_leaderboard()
            st.cache_data.clear()
            st.success(f'Saved {custom["run_id"]}: net ₹{custom["metrics"]["net_pnl"]:,.2f}, unresolved {custom["metrics"]["unresolved_trades"]}, modeled trades {custom["metrics"]["modeled_trades"]}. Refresh results to select this run.')
        except (ValueError,OSError) as exc: st.error(str(exc))

with tabs[1]:
    if selected:
        equity=table(selected,'equity'); monthly=table(selected,'monthly'); trades=table(selected,'trades')
        st.subheader('All-trade equity & realized drawdown')
        st.caption('All entered trades close; estimated prices contribute when the run is model-dependent. This is realized daily drawdown, not intraday mark-to-market risk. Zero-P&L dates include excluded sessions.')
        fig=make_subplots(rows=2,cols=1,shared_xaxes=True,row_heights=[.7,.3],vertical_spacing=.05)
        fig.add_trace(go.Scatter(x=equity.date,y=equity.equity,line_color='#36cfaf',name='Research equity'),row=1,col=1)
        fig.add_trace(go.Scatter(x=equity.date,y=equity.drawdown,fill='tozeroy',line_color='#ff6685',name='Realized drawdown'),row=2,col=1)
        st.plotly_chart(theme(fig,500),width='stretch')
        a,b=st.columns(2)
        with a:
            st.subheader('Monthly P&L / initial capital (%)')
            heat=monthly.copy(); heat['year']=heat.month.str[:4]; heat['month_number']=heat.month.str[5:7].astype(int)
            pivot=heat.pivot(index='year',columns='month_number',values='return_pct').reindex(columns=range(1,13))
            st.plotly_chart(theme(go.Figure(go.Heatmap(z=pivot.values,x=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'],y=pivot.index,
                                                       colorscale='RdBu',zmid=0,text=np.round(pivot.values,2),texttemplate='%{text}',hoverongaps=False)),280),width='stretch')
        with b:
            st.subheader('Day of week: net P&L')
            day=equity.copy(); day['weekday']=pd.to_datetime(day.date).dt.day_name()
            days=day.groupby('weekday').net_pnl.sum().reindex(['Monday','Tuesday','Wednesday','Thursday','Friday']).fillna(0)
            st.plotly_chart(theme(px.bar(x=days.index,y=days.values,labels={'x':'','y':'₹'},color_discrete_sequence=['#758cff']),280),width='stretch')
        a,b=st.columns(2)
        with a:
            st.subheader('CE / PE / futures split')
            st.dataframe(pd.DataFrame(r['splits'].get('side',[])),hide_index=True,width='stretch')
        with b:
            st.subheader('Expiry-day edge')
            st.caption('Underlying expiry-session classification uses dated rules and holidays. Calendar flags and empirical IV crush do not verify the expiry identity of rolling candles.')
            st.dataframe(pd.DataFrame(r['splits'].get('expiry_day',[])),hide_index=True,width='stretch')
        st.json({k:m[k] for k in ['roi_pct','expectancy','sharpe','calmar','total_cost','train_2025_net','validation_2026_net']})
    else: st.info('Select a saved backtest to view analytics.')

with tabs[2]:
    st.subheader('Actual-candle trade replay')
    if selected:
        trades=table(selected,'trades')
        if trades.empty: st.info('This configuration generated no trades.')
        else:
            ledger=trade_ledger(trades)
            st.dataframe(ledger,hide_index=True,width='stretch')
            st.caption('Trade ROI = net P&L / premium or assumed futures margin committed. Capital used % uses account equity at entry. Fees are reserved in addition to allocated capital; old-engine runs show N/A where allocation was not recorded.')
            st.download_button('Export comprehensive ledger',ledger.to_csv(index=False),f'{selected}_ledger.csv','text/csv')
            with st.expander('Raw accounting and signal evidence'): st.dataframe(trades,hide_index=True,width='stretch')
            trade_id=st.selectbox('Trade',trades.trade_id.tolist(),format_func=lambda v: f'{v} · {trades.loc[trades.trade_id.eq(v),"date"].iloc[0]} · {trades.loc[trades.trade_id.eq(v),"side"].iloc[0]}')
            tr=trades.loc[trades.trade_id.eq(trade_id)].iloc[0]
            end_date=str(pd.Timestamp(tr.date).date()+timedelta(days=1))
            is_mcx=c['instrument'] in POINT_VALUE
            spot=query(tr.contract if is_mcx else c['instrument'],tr.date,end_date)
            bars=make_bars(spot,c['timeframe'])
            if is_mcx: execution=spot
            else:
                raw=pd.concat([query(f'{c["instrument"]}_{tr.side}_{k}_WEEK1',tr.date,end_date,'options_rolling_1m') for k in ['ATM','ATM-1','ATM+1']],ignore_index=True)
                execution=raw[raw.strike.eq(tr.strike)].drop_duplicates('timestamp').sort_values('timestamp')
            paths=table(selected,'stop_paths'); paths=paths[paths.trade_id.eq(trade_id)] if not paths.empty else paths
            fig=make_subplots(rows=2,cols=1,shared_xaxes=True,vertical_spacing=.06,subplot_titles=['Underlying · completed signal bars',f'Execution candles · {tr.side} · strike {tr.strike:g}' if not is_mcx else 'Execution candles · fixed futures contract'])
            candles(fig,bars,1,'Underlying')
            for name,color in [('ema9','#7597ff'),('ema21','#bc8cff'),('vwap','#f5bc68'),('supertrend','#8bc4a2')]:
                fig.add_trace(go.Scatter(x=bars.timestamp,y=bars[name],name='TWAP proxy' if name=='vwap' and bool(bars.vwap_proxy.all()) else name,line=dict(color=color,width=1)),row=1,col=1)
            candles(fig,execution,2,'Actual execution instrument')
            if not is_mcx and not execution.empty:
                pv=((execution.high+execution.low+execution.close)/3*execution.volume).cumsum()
                option_vwap=pv/execution.volume.cumsum().replace(0,np.nan)
                st.caption('Option VWAP below uses observed fixed-strike candles. Missing intervals are not synthesized into VWAP; H1 additionally requires complete session history before entry.')
                fig.add_trace(go.Scatter(x=execution.timestamp,y=option_vwap,name='Observed option VWAP',line=dict(color='#ffe099')),row=2,col=1)
            if not paths.empty:
                if 'price_source' in paths:
                    modeled=paths[paths.price_source.ne('observed')]
                    if not modeled.empty:
                        fig.add_trace(go.Candlestick(x=modeled.timestamp,open=modeled.open,high=modeled.high,low=modeled.low,close=modeled.close,
                            name='MODELED missing-strike prices',increasing_line_color='#bf8bff',decreasing_line_color='#bf8bff'),row=2,col=1)
                        st.warning(f'This trade used {len(modeled)} modeled candles. The purple candles are estimates, not historical quotes.')
                fig.add_trace(go.Scatter(x=paths.timestamp,y=paths.stop,name='Active stop (next-bar updates)',line=dict(color='#ff6685',shape='hv')),row=2,col=1)
                fig.add_trace(go.Scatter(x=paths.timestamp,y=paths.target,name='Target',line=dict(color='#f5bc68',dash='dot')),row=2,col=1)
            fig.add_trace(go.Scatter(x=[tr.entry_time],y=[tr.entry_price],name='Entry incl. slippage',mode='markers',marker=dict(color='#36cfaf',size=13,symbol='triangle-up')),row=2,col=1)
            if tr.status=='closed':
                fig.add_trace(go.Scatter(x=[tr.exit_time],y=[tr.exit_price],name='Exit incl. slippage',mode='markers',marker=dict(color='#ff6685',size=13,symbol='triangle-down')),row=2,col=1)
            else: st.error('Censored position: entered strike disappeared. No exit price or net P&L is invented.')
            fig.update_xaxes(rangeslider_visible=False)
            st.plotly_chart(theme(fig,780),width='stretch')
            st.caption('Axes are synchronized. Fixed-strike observations are combined across all three streams. Purple candles explicitly show missing-price estimates. Expiry identity remains unverified. Entry/exit markers include slippage.')
            if not is_mcx: st.dataframe(execution[['timestamp','strike','volume','oi','iv']],hide_index=True,width='stretch')
            st.download_button('Export trade log',trades.to_csv(index=False),f'{selected}_trades.csv','text/csv')
    else: st.info('Select a saved backtest to inspect trades.')

with tabs[3]:
    st.subheader('Data lake coverage & quality')
    summary_path=ROOT/'summary.csv'
    if summary_path.exists(): st.dataframe(pd.read_csv(summary_path),hide_index=True,width='stretch')
    qp=ROOT/'quality_report.json'
    if qp.exists(): st.json(json.loads(qp.read_text()),expanded=False)
    audit_path=OUT/'option_data_audit.json'
    if audit_path.exists():
        st.subheader('Expiry & continuity model diagnostics')
        st.json(json.loads(audit_path.read_text()),expanded=False)
        diagnostic_name=st.selectbox('Expiry diagnostic instrument',['NIFTY','SENSEX'])
        diagnostic_path=OUT/f'expiry_diagnostics_{diagnostic_name}.parquet'
        if diagnostic_path.exists(): st.dataframe(pd.read_parquet(diagnostic_path),hide_index=True,width='stretch')
    if selected:
        st.caption(f'Run coverage: {r["first_session"]} → {r["last_session"]}. Exclusions are data-quality eligibility filters and may create sample-selection bias.')
        st.dataframe(table(selected,'excluded_sessions'),hide_index=True,width='stretch')
    with QuantDataLake() as lake: catalog=lake.instruments()
    ds_name=st.selectbox('Raw dataset / instrument',catalog,format_func=lambda v:f'{v[0]} / {v[1]}')
    raw_day=st.date_input('Raw session date',date(2026,10,22),key='raw_date')
    if ds_name:
        ds,name=ds_name
        if ds=='daily':
            with QuantDataLake() as lake: raw=lake.query(name,str(raw_day),str(raw_day+timedelta(days=1)),timeframe='1d',dataset=ds)
        else: raw=query(name,str(raw_day),str(raw_day+timedelta(days=1)),ds)
        st.dataframe(raw,hide_index=True,width='stretch')
        if name in ('NIFTY','SENSEX'):
            with st.expander('Same-day rolling CE / PE candles (all three moneyness streams)'):
                options=pd.concat([query(f'{name}_{side}_{k}_WEEK1',str(raw_day),str(raw_day+timedelta(days=1)),'options_rolling_1m') for side in ('CE','PE') for k in ('ATM','ATM-1','ATM+1')],ignore_index=True)
                st.dataframe(options,hide_index=True,width='stretch')
        st.caption('These are rolling option candles, not a full historical option-chain snapshot.')

with tabs[4]:
    render_live_paper_room(ROOT / 'paper_trades')

with tabs[5]:
    render_option_chain(ROOT)
