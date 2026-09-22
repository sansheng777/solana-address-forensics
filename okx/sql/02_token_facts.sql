-- 02_token_facts — turn the coins and pools from step one into metadata (all three launchpads)
--
-- Usage: mints / pools must be **explicit literal lists**, never nested sub-queries (measured 6x
--        slower). The date window has to cover the creation time, normally 7 days before the fill
--        window (these coins are short-lived)
--
-- ★ The three launchpads expose different things. What is missing is stated as missing:
--   pump       the creation table gives everything: initial curve supply / developer / name /
--              metadata / mayhem / inner address; graduation comes from the pool-creation table
--   DBC        the pool-creation table gives: coin address / pool / shop (config) / creator;
--              graduation comes from evt_evtcurvecomplete
--              ⚠️ no name, metadata or initial curve supply — DBC's position is measured against the
--              fundraising target, so it never needed an initial supply
--   LaunchLab  ★ Corrected 2026-09-19: **Dune decodes 26 tables, not 2.** The earlier claim that only
--              fills and migration were decoded came from an incomplete snapshot of the table list and
--              was wrong.
--              `raydium_launchpad_evt_poolcreateevent` gives the creation time, **the creator (dev)**,
--              the shop (config) and base_mint_param (with decimals, name and symbol), matched on
--              pool_state.
--              → LaunchLab's dev and creation time **are available**; the old "unavailable" note is
--              void.
--              ⚠️ It still does not give the **coin address** (only pool_state), which continues to
--              come from the migration table or from 03 by transaction id.
with pf as (
    select 'pumpfun' as road, mint as token, cast(null as varchar) as pool,
           symbol, name, creator as dev, uri,
           real_token_reserves / 1e6 as curve0, is_mayhem_mode as is_mayhem,
           bonding_curve as inner_addr, cast(null as varchar) as platform,
           cast(null as varchar) as cfg,
           evt_block_time as created_at
    from pumpdotfun_solana.pump_evt_createevent
    where evt_block_date between date '{d0}' and date '{d1}' and mint in ({mints})
), pf_g as (
    select base_mint as token, pool as outer_addr, coin_creator as dev2, evt_block_time as graduated_at
    from pumpdotfun_solana.pump_amm_evt_createpoolevent
    where evt_block_date between date '{d0}' and date '{d1}'
      and (base_mint in ({mints}) or pool in ({pools}))
), dbc_cfg as (          -- DBC's shop template: which token this shop quotes in (absent from the
                         -- fill table, so this is the only source)
    select account_config as config, max(account_quote_mint) as quote_mint
    from meteora_solana.dynamic_bonding_curve_call_create_config
    where call_block_date between date '{cfg0}' and date '{d1}'
    group by 1
), dbc as (
    select 'dbc' as road, base_mint as token, pool,
           cast(null as varchar) as symbol, cast(null as varchar) as name,
           creator as dev, cast(null as varchar) as uri,
           cast(null as double) as curve0, cast(null as boolean) as is_mayhem,
           pool as inner_addr, config as platform,          -- DBC's shop = config
           config as cfg,
           evt_block_time as created_at
    from meteora_solana.dynamic_bonding_curve_evt_evtinitializepool
    where evt_block_date between date '{d0}' and date '{d1}'
      and (base_mint in ({mints}) or pool in ({pools}))
), dbc_g as (
    select pool, evt_block_time as graduated_at
    from meteora_solana.dynamic_bonding_curve_evt_evtcurvecomplete
    where evt_block_date between date '{d0}' and date '{d1}' and pool in ({pools})
), ll_c as (          -- ★ Added 2026-09-19: LaunchLab pool-creation events (previously believed to be
                      -- undecoded)
    select pool_state as pool, creator as dev, config as platform,
           evt_block_time as created_at,
           json_extract_scalar(base_mint_param, '$.MintParams.name')   as name,
           json_extract_scalar(base_mint_param, '$.MintParams.symbol') as symbol,
           try_cast(json_extract_scalar(base_mint_param, '$.MintParams.decimals') as integer) as base_dec
    from raydium_solana.raydium_launchpad_evt_poolcreateevent
    where evt_block_date between date '{d0}' and date '{d1}'
      and pool_state in ({pools})
), ll_g as (
    select account_base_mint as token, account_pool_state as pool,
           account_platform_config as platform,             -- LaunchLab's shop, e.g. stonk.fun
           account_quote_mint as quote_mint,
           call_block_time as graduated_at
    from raydium_solana.raydium_launchpad_call_migrate_to_cpswap
    where call_block_date between date '{d0}' and date '{d1}'
      and (account_base_mint in ({mints}) or account_pool_state in ({pools}))
)
select p.road, p.token, p.pool, p.symbol, p.name, p.dev, g.dev2, p.uri,
       p.curve0, p.is_mayhem, p.inner_addr, p.platform, p.created_at,
       g.outer_addr, g.graduated_at, g.graduated_at is not null as graduated,
       cast(null as varchar) as quote_mint,
       cast(null as integer) as base_dec
from pf p left join pf_g g on g.token = p.token
union all
select d.road, d.token, d.pool, d.symbol, d.name, d.dev, cast(null as varchar), d.uri,
       d.curve0, d.is_mayhem, d.inner_addr, d.platform, d.created_at,
       cast(null as varchar), dg.graduated_at, dg.graduated_at is not null,
       c.quote_mint,                                   -- ★ DBC's quote token, needed for USD conversion
       cast(null as integer)
from dbc d left join dbc_g dg on dg.pool = d.pool
           left join dbc_cfg c on c.config = d.cfg
union all
-- ★ full outer: either side can appear alone (a coin that never graduated has only the creation
--   event; one created before the window has only the migration row)
select 'launchlab', l.token, coalesce(c.pool, l.pool), c.symbol, c.name,
       c.dev,                                          -- ★ the creator, previously recorded as unavailable
       cast(null as varchar), cast(null as varchar),
       cast(null as double), cast(null as boolean),
       coalesce(c.pool, l.pool), coalesce(l.platform, c.platform),
       c.created_at,                                   -- ★ the creation time, previously recorded as
                                                       -- unavailable
       cast(null as varchar), l.graduated_at, l.graduated_at is not null,
       l.quote_mint,
       c.base_dec
from ll_c c full outer join ll_g l on l.pool = c.pool
