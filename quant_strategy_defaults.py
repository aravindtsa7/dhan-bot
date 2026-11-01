"""UI defaults, independently importable during a Streamlit hot reload."""
def strategy_defaults(instrument,strategy):
    from quant_strategy_models import DIRECTIONAL_OPTIONS
    mcx=instrument in ('NATURALGAS','CRUDEOIL','GOLDM')
    values=dict(capital={'NATURALGAS':2_000_000,'CRUDEOIL':4_000_000,'GOLDM':8_000_000}.get(instrument,100_000),
                allocation='25%',take_profit='25%',stop_loss='15%',minimum_premium=0. if mcx else 50.)
    if strategy=='expiry_day':
        values.update(take_profit='25%',stop_loss='60%',minimum_premium=70.,allocation='50%')
    elif strategy=='expiry_intraday':
        values.update(take_profit='35%',stop_loss='40%',minimum_premium=40.,allocation='50%')
    elif mcx:values.update(take_profit='30%',stop_loss='15%')
    elif strategy in DIRECTIONAL_OPTIONS:values.update(take_profit='30%',stop_loss='20%')
    return values
