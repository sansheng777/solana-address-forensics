-- 07_derived_prices — derive a USD price for quote tokens that have no official price feed
--
-- How it works: these tokens (xSOL / STONK / ANSEM ...) trade against SOL on DEXes themselves, and
--   Dune computes amount_usd for a fill as long as one side has a price (SOL does) → dividing by the
--   token amount gives its unit price. In effect SOL is used as a bridge: token → SOL → USD.
-- Usage: `mints` is a literal list (the quote tokens that got no free price from 03's transfers)
select mint, hour, sum(usd) / nullif(sum(amt), 0) as price, sum(usd) as usd_vol
from (
    select token_bought_mint_address as mint,
           date_trunc('hour', block_time) as hour,
           token_bought_amount as amt, amount_usd as usd
    from dex_solana.trades
    where block_date between date '{d0}' and date '{d1}'
      and token_bought_mint_address in ({mints})
      and amount_usd > 0 and token_bought_amount > 0
    union all
    select token_sold_mint_address, date_trunc('hour', block_time),
           token_sold_amount, amount_usd
    from dex_solana.trades
    where block_date between date '{d0}' and date '{d1}'
      and token_sold_mint_address in ({mints})
      and amount_usd > 0 and token_sold_amount > 0
)
group by 1, 2
