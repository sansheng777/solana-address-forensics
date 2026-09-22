-- 09_token_decimals — authoritative token decimals, so they no longer have to be inferred from
-- amount ratios
--
-- ★ Added 2026-09-19. store.py used to infer decimals as "raw fill value ÷ the scaled transfer of the
--   same coin in the same transaction = 10^d", and any fill it could not infer was excluded from the
--   PnL — measured on address 2, that was **181 of 1307 fills (13.8%)**.
--   The root cause was ignoring the project's first rule: **Dune already has a decimals table**.
--
-- Both tables are needed (their fields were verified identical 2026-09-19):
--   tokens_solana.fungible          classic SPL plus tokens with metadata created
--   tokens_solana.fungible_static   includes token_version = 'spl_token_2022'
--   Both carry decimals / created_at / symbol / name / token_uri / init_tx.
--
-- ⚠️ A token can still be missing here (a very new coin may not have reached either table yet), so
--    store.py keeps the old inference as a fallback and only marks amt_display=False when both fail.
--    **Table first, inference as backup** — not the other way round.
select token_mint_address as mint,
       max(decimals)      as decimals,
       max(symbol)        as symbol,
       max(name)          as name,
       max(created_at)    as created_at        -- ★ also: the token's birth time, which cross-checks
                                               --   LaunchLab coin ages
from (
    select token_mint_address, decimals, symbol, name, created_at
    from tokens_solana.fungible          where token_mint_address in ({mints})
    union all
    select token_mint_address, decimals, symbol, name, created_at
    from tokens_solana.fungible_static   where token_mint_address in ({mints})
)
group by 1
