-- 01_address_trades — every fill this address made on the three SOL launchpads and their post-graduation venues
--
-- Coverage evidence (measured 2026-09-17 over 165,123 launchpad traders): covering pump alone gives
--   68.8% complete reports / 16.6% missing fills / **14.5% answering "no activity found"** → all three
--   roads are mandatory
--
-- ★ How progress is measured differs per road — DBC and LaunchLab **carry their own denominator**,
--   while pump needs a join against the creation table
--     pump       curve_left = curve supply left after the fill; denominator = real_token_reserves from
--                the creation table (store.py does the division)
--     DBC        quote_reserve_amount / migration_threshold   (raised-funds basis, threshold per fill)
--     launchlab  real_base_after / total_base_sell            (supply basis, 793.1M on a standard pool)
--   progress_basis always travels with the number: supply and quote bases cannot be compared for
--   "how far along" directly
--
-- ★ Trade direction: pump uses the boolean is_buy; **DBC's trade_direction is 0=sell 1=buy
--   (counter-intuitive, verified)**; LaunchLab is plain JSON, read $.TradeDirection = Buy/Sell
--
-- ★ Signer-only lookup (simplified 2026-09-19) — the where clause used to also carry "or tx_id is in
--   the set query 03 returned", to recover the "someone else signed, he received the coins" case.
--   **Measured after removing it, not one fill is lost:**
--     address 1 pump 911 fills / address 2 launchlab 1307 / address 3 dbc 2731,
--     **100% sit in transactions he signed himself; delegated-signed txs contributed 0 fills.**
--   Those delegated transfers do exist (46 / 270 / 659 of them), but their router is always the Token
--   Program and none of them lands in a transaction that has a fill — they are airdrops and transfers,
--   not trades.
--   (Address 3 was therefore gaining 445 coins with "a record but no fills", 74% of its coin list.)
--   It also removed the tx literal list that appeared 8 times in this query and hit Dune's 500,000
--   character limit after a few thousand fills.
--   ⚠️ What would overturn this: an address whose fill count is visibly short while its
--   delegated-signed transfers do land in transactions that have fills.
-- ★ The trader is always evt_tx_signer; never dex_solana/base_trades' trader_id (≥64% of it is routers)
-- ★ LaunchLab's fill table carries pool_state but no coin address → the coin is filled in from query
--   03's transfer table by tx_id
-- ★ Amounts: pump is already scaled by decimals; **DBC / LaunchLab give raw values** — their fill
--   tables carry no quote_mint, and the three roads together use 543 different quote tokens with
--   different decimals. PnL is therefore [grouped by coin, never summed across quote tokens], plus a
--   dimensionless multiple (received / spent) that decimals cannot affect.

-- ★ fee_quote (added 2026-09-19) — its unit matches this branch's amt_quote exactly (raw stays raw,
--   scaled stays scaled)
--
-- ‼️ Three **nested** fee traps; adding them up double-counts. All three verified against real data
--    on 2026-09-19:
--   · pump's buyback_fee = 50% of fee      (6 sample rows, buyback/fee is always 0.500, bp=5000) → **not added**
--   · pump_amm the same                    (protocol 504184 / buyback 252092)                     → **not added**
--   · DBC's protocol_fee = 25% of trading_fee (47360/189444 and 26581/106326, both exactly .250)   → **not added**
-- ‼️ pump's creator_fee is asymmetric: **30bp on a buy, 300bp on a sell**; fee is always 95bp.
--    → pump's real rate is 1.25% buying / 3.95% selling. LaunchLab is protocol 0.25% + platform 1.00% = 1.25%.
-- ‼️‼️ **fee_basis: is the fee inside amt_quote or not** — found 2026-09-19 while cross-checking against
--    GMGN smart money: we were **subtracting the fee twice** on pump's outer venue and on LaunchLab
--    (5 days computed to 1,670 where GMGN's 7 days was 873).
--    How to tell: **check whether the buy and sell rates are symmetric.**
--      the amount is the [pool-side gross] → fee/amount identical both ways → fee_basis='on_top', **subtract it**
--      the amount is [user-side]          → the sell side already had the fee taken out,
--                                           so fee/net = r/(1-r), higher than the buy side
--                                                                → fee_basis='in_amount', **do not subtract again**
--    | Road | Buy | Sell | Verdict |
--    |---|---|---|---|
--    | pump inner | 1.2500% | 1.2500% | on_top (PERKFi: 0.195555555+0.002444445 = **0.198**, a round order size) |
--    | pump outer | 1.1858% | 1.2146% | **in_amount** (user_quote_amount_in/out, confirmed directly 4/4) |
--    | LaunchLab  | 1.2500% | **1.2658%** | **in_amount** (1.25/(1-1.25%) = 1.2658, an exact match) |
--    | DBC inner/outer | 0.2005% / 0.0801% | same | on_top (perfectly symmetric) |
--
-- ‼️ **The fee is not always charged in the quote token** — this one nearly corrupted the whole DBC
--    road on 2026-09-19:
--    · pump / LaunchLab: always in the [quote token]. Measured rates are stable both ways
--      (pump 1.25%/3.95%, LL 1.25%)
--    · **DBC (both the inner evtswap2 and the outer cp_amm): the fee is charged on the [output token]**
--        selling → the output is the quote token → fee in quote (measured 0.2005% / 0.0801%, very stable)
--        buying  → the output is the base coin   → **fee in base**
--      Treating it as quote gives a buy-side rate of 433% (raw 6.307e8 / quote 1.339e8); dividing the
--      same number by the raw base amount 3.1454e11 gives exactly 0.2005% — the same rate as the sell
--      side, which settles it.
--      → an extra column fee_side ('quote' / 'base'); store.py converts according to it.
--
-- ⚠️ LaunchLab's outer venue (raydium_cp_swap_call_*) **has no fee field at all** — it is a call table
--    (decoded instruction arguments), which does not carry settlement results. fee_quote is NULL on
--    that road and store.py records a fee_missing_reason; **it is not treated as 0**.
with pf as (                                  -- pump.fun inner venue
    select 'pumpfun' as road, 'inner' as venue,
           t.mint as token, cast(null as varchar) as pool,
           t.evt_tx_signer as signer, t.evt_outer_executing_account as router,
           t.is_buy as is_buy,
           t.token_amount / 1e6 as amt_token,
           cast(t.quote_amount as double) / power(10, coalesce(fg.decimals, 9)) as amt_quote,
           coalesce(fg.symbol, 'SOL') as quote_symbol, t.quote_mint as quote_mint,
           t.real_token_reserves / 1e6 as curve_left,     -- store.py takes the denominator from the creation table
           cast(null as double) as progress,
           'supply' as progress_basis,
           t.mayhem_mode as is_mayhem,
           t.evt_block_time, t.evt_block_slot, t.evt_tx_index, t.evt_outer_instruction_index, t.evt_tx_id,
           (coalesce(cast(t.fee as double), 0) + coalesce(cast(t.creator_fee as double), 0))
               / power(10, coalesce(fg.decimals, 9)) as fee_quote,      -- ★ buyback excluded (it is half of fee)
           'quote' as fee_side,
           'on_top' as fee_basis                     -- pool-side amount, fee sits outside it, subtract it
    from pumpdotfun_solana.pump_evt_tradeevent t
    left join tokens_solana.fungible fg on fg.token_mint_address = t.quote_mint
    where t.evt_block_date between date '{d0}' and date '{d1}' and t.evt_tx_signer = '{addr}'
), pa_b as (                                  -- pump outer venue, buys
    select 'pumpfun' as road, 'outer' as venue,
           cast(null as varchar) as token, b.pool as pool,
           b.evt_tx_signer, b.evt_outer_executing_account, true as is_buy,
           b.base_amount_out / 1e6 as amt_token, b.user_quote_amount_in / 1e9 as amt_quote,
           'SOL' as quote_symbol, cast(null as varchar) as quote_mint,
           cast(null as double) as curve_left, cast(null as double) as progress,
           cast(null as varchar) as progress_basis, cast(null as boolean) as is_mayhem,
           b.evt_block_time, b.evt_block_slot, b.evt_tx_index, b.evt_outer_instruction_index, b.evt_tx_id,
           (coalesce(cast(b.lp_fee as double), 0) + coalesce(cast(b.protocol_fee as double), 0)
            + coalesce(cast(b.coin_creator_fee as double), 0)) / 1e9, 'quote',
           'in_amount'                               -- ★ user_quote_amount_in already includes the fee, do not subtract
    from pumpdotfun_solana.pump_amm_evt_buyevent b
    where b.evt_block_date between date '{d0}' and date '{d1}' and b.evt_tx_signer = '{addr}'
), pa_s as (                                  -- pump outer venue, sells
    select 'pumpfun', 'outer', cast(null as varchar), s.pool,
           s.evt_tx_signer, s.evt_outer_executing_account, false,
           s.base_amount_in / 1e6, s.user_quote_amount_out / 1e9,
           'SOL', cast(null as varchar),
           cast(null as double), cast(null as double), cast(null as varchar), cast(null as boolean),
           s.evt_block_time, s.evt_block_slot, s.evt_tx_index, s.evt_outer_instruction_index, s.evt_tx_id,
           (coalesce(cast(s.lp_fee as double), 0) + coalesce(cast(s.protocol_fee as double), 0)
            + coalesce(cast(s.coin_creator_fee as double), 0)) / 1e9, 'quote',  -- buyback excluded
           'in_amount'                               -- ★ user_quote_amount_out already had the fee taken out
    from pumpdotfun_solana.pump_amm_evt_sellevent s
    where s.evt_block_date between date '{d0}' and date '{d1}' and s.evt_tx_signer = '{addr}'
), dbc as (                                   -- Meteora DBC inner venue
    select 'dbc', 'inner', cast(null as varchar), d.pool,
           d.evt_tx_signer, d.evt_outer_executing_account,
           d.trade_direction = 1,                                        -- ⚠️ 0=sell 1=buy
           -- buy: input=money output=goods; sell: the other way round.
           -- ⚠️ Raw values; the decimals depend on that pool's quote token (absent from the fill table)
           if(d.trade_direction = 1, cast(json_value(d.swap_result, 'strict $.SwapResult2.output_amount') as double),
                                     cast(json_value(d.swap_parameters, 'strict $.SwapParameters2.amount_0') as double)),
           if(d.trade_direction = 1, cast(json_value(d.swap_parameters, 'strict $.SwapParameters2.amount_0') as double),
                                     cast(json_value(d.swap_result, 'strict $.SwapResult2.output_amount') as double)),
           cast(null as varchar), cast(null as varchar),
           cast(null as double),
           cast(d.quote_reserve_amount as double) / nullif(cast(d.migration_threshold as double), 0),
           'quote', cast(null as boolean),
           d.evt_block_time, d.evt_block_slot, d.evt_tx_index, d.evt_outer_instruction_index, d.evt_tx_id,
           coalesce(cast(json_value(d.swap_result, 'strict $.SwapResult2.trading_fee') as double), 0)
           + coalesce(cast(json_value(d.swap_result, 'strict $.SwapResult2.referral_fee') as double), 0)
           ,                                                            -- protocol_fee excluded (25% of trading_fee)
           if(d.trade_direction = 1, 'base', 'quote'),                   -- ★ the fee is charged on the output token
           'on_top'
    from meteora_solana.dynamic_bonding_curve_evt_evtswap2 d
    where d.evt_block_date between date '{d0}' and date '{d1}' and d.evt_tx_signer = '{addr}'
), ll as (                                    -- Raydium LaunchLab inner venue
    select 'launchlab', 'inner', cast(null as varchar), l.pool_state,
           l.evt_tx_signer, l.evt_outer_executing_account,
           json_value(l.trade_direction, 'strict $.TradeDirection') = 'Buy',
           -- buy: amount_in=money amount_out=goods; sell: the other way round. ⚠️ Raw values again
           if(json_value(l.trade_direction, 'strict $.TradeDirection') = 'Buy',
              cast(l.amount_out as double), cast(l.amount_in as double)),
           if(json_value(l.trade_direction, 'strict $.TradeDirection') = 'Buy',
              cast(l.amount_in as double), cast(l.amount_out as double)),
           cast(null as varchar), cast(null as varchar),
           cast(null as double),
           cast(l.real_base_after as double) / nullif(cast(l.total_base_sell as double), 0),
           'supply', cast(null as boolean),
           l.evt_block_time, l.evt_block_slot, l.evt_tx_index, l.evt_outer_instruction_index, l.evt_tx_id,
           coalesce(cast(l.protocol_fee as double), 0) + coalesce(cast(l.platform_fee as double), 0)
           + coalesce(cast(l.creator_fee as double), 0) + coalesce(cast(l.share_fee as double), 0), 'quote',
           'in_amount'                               -- ★ amount_in/out are user-side, do not subtract again
    from raydium_solana.raydium_launchpad_evt_tradeevent l
    where l.evt_block_date between date '{d0}' and date '{d1}' and l.evt_tx_signer = '{addr}'
), dbc_o as (             -- DBC's post-graduation venue: Meteora cp-amm (DAMM v2)
    -- Measured 2026-09-18: one DBC trader had 9 coins with "transfers but no fills", all of them here
    -- (same counterparty address, project=meteora, trade_source=cpamdpZ…)
    select 'dbc', 'outer', cast(null as varchar), o.pool,
           o.evt_tx_signer, o.evt_outer_executing_account,
           o.trade_direction = 1,                                     -- same direction convention as DBC inner
           -- ⚠️ Both sides must be flipped by direction: buy input=money output=goods; sell the other way.
           --    Hard-coding one way produces absurd numbers like 916x (measured 2026-09-18)
           if(o.trade_direction = 1, cast(o.excluded_transfer_fee_amount_out as double),
                                     cast(o.included_transfer_fee_amount_in as double)),
           if(o.trade_direction = 1, cast(o.included_transfer_fee_amount_in as double),
                                     cast(o.excluded_transfer_fee_amount_out as double)),
           cast(null as varchar), cast(null as varchar),
           cast(null as double), cast(null as double),                -- no curve off the launchpad, progress is meaningless
           cast(null as varchar), cast(null as boolean),
           o.evt_block_time, o.evt_block_slot, o.evt_tx_index, o.evt_outer_instruction_index, o.evt_tx_id,
           coalesce(cast(json_value(o.swap_result, 'strict $.SwapResult2.trading_fee') as double), 0)
           + coalesce(cast(json_value(o.swap_result, 'strict $.SwapResult2.referral_fee') as double), 0),
           if(o.trade_direction = 1, 'base', 'quote'),                   -- ★ as in DBC inner
           'on_top'
    from meteora_solana.cp_amm_evt_evtswap2 o
    where o.evt_block_date between date '{d0}' and date '{d1}' and o.evt_tx_signer = '{addr}'
), ll_o as (              -- LaunchLab's post-graduation venue: Raydium cpswap
    -- ⚠️ This is a call table, not an event table: only one of amountIn / amountOut is the real filled
    --    amount, the other is a slippage bound (minimumAmountOut / maximumAmountIn) and must not be
    --    used as an amount.
    -- ★ In exchange it gives the [coin addresses] on both sides directly, which beats a pool address:
    --    he pays inputTokenMint and receives outputTokenMint.
    --    Everything here is recorded as "a buy of outputTokenMint"; store.py flips the direction based
    --    on whether what he received is the quote token.
    select 'launchlab', 'outer', x.account_outputTokenMint, x.account_poolState,
           x.call_tx_signer, x.call_outer_executing_account,
           true,
           cast(null as double),                                  -- how much was received (no real value in a call table)
           cast(x.amountIn as double),                            -- how much was paid (real)
           cast(null as varchar), x.account_inputTokenMint,       -- what was paid = the quote token
           cast(null as double), cast(null as double),            -- no curve off the launchpad
           cast(null as varchar), cast(null as boolean),
           x.call_block_time, x.call_block_slot, x.call_tx_index, x.call_outer_instruction_index, x.call_tx_id,
           cast(null as double),                         -- a call table has no fee field; **not treated as 0**
           cast(null as varchar), cast(null as varchar)
    from raydium_cp_solana.raydium_cp_swap_call_swapbaseinput x
    where x.call_block_date between date '{d0}' and date '{d1}' and x.call_tx_signer = '{addr}'
    union all
    select 'launchlab', 'outer', y.account_outputTokenMint, y.account_poolState,
           y.call_tx_signer, y.call_outer_executing_account,
           true,
           cast(y.amountOut as double),                           -- how much was received (real)
           cast(null as double),
           cast(null as varchar), y.account_inputTokenMint,
           cast(null as double), cast(null as double),
           cast(null as varchar), cast(null as boolean),
           y.call_block_time, y.call_block_slot, y.call_tx_index, y.call_outer_instruction_index, y.call_tx_id,
           cast(null as double), cast(null as varchar), cast(null as varchar)
    from raydium_cp_solana.raydium_cp_swap_call_swapbaseoutput y
    where y.call_block_date between date '{d0}' and date '{d1}' and y.call_tx_signer = '{addr}'
)
select * from (
    select * from pf union all select * from pa_b union all select * from pa_s
    union all select * from dbc union all select * from ll
    union all select * from dbc_o union all select * from ll_o)
order by evt_block_slot, evt_tx_index, evt_outer_instruction_index
