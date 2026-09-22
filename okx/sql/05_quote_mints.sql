-- One-off: run it once, store the result in okx/data/quote_mints.json and read it locally afterwards.
--    Its purpose is **identifying which token a payment was made in** during amount matching, and
--    **not** deciding "is this money" — that question is answered only by the 5 entries in store.py's
--    MONEY list (STONK and USELESS appear in this table but are traded coins themselves).

-- 05_quote_mints — the quote-token registry: which tokens act as "money" rather than "goods"
--
-- Why it is needed: computing PnL requires telling the goods leg from the money leg, and there is no
--   ready-made field saying "this token is a quote token". But each launchpad's own tables record
--   what it quotes in, so their union is the full set.
--   Hit 2026-09-18: without that separation, WETH / ZEC / USDC were treated as meme coins and summed
--   into an absurd +31,166,375 across different currencies.
-- Usage: run once, store it locally (okx/data/quote_mints.json) and use it as a literal list.
select mint, array_agg(distinct src) as sources, sum(n) as n_seen
from (
    select quote_mint as mint, 'pumpfun' as src, count(*) as n
    from pumpdotfun_solana.pump_evt_tradeevent
    where evt_block_date between date '{d0}' and date '{d1}'
    group by 1
    union all
    select account_quote_mint, 'dbc', count(*)
    from meteora_solana.dynamic_bonding_curve_call_create_config
    where call_block_date between date '{d0}' and date '{d1}'
    group by 1
    union all
    select account_quote_mint, 'launchlab', count(*)
    from raydium_solana.raydium_launchpad_call_migrate_to_cpswap
    where call_block_date between date '{d0}' and date '{d1}'
    group by 1
)
where mint is not null
group by 1 order by 3 desc
