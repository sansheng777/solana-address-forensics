-- 03_address_receipts — the attribution layer, and the foundation of the whole product: which tokens
-- this address actually received and paid out
--
-- ★ Both transfer tables must be unioned: Token-2022 and classic SPL.
--   Measured 2026-09-17: of pump's 53,217 coins, 50,942 are Token-2022 and **2,275 (4.3%) are classic
--   SPL**; DBC supports both (its pool-creation table has with_spl_token and with_token2022 variants).
--   Reading only one of them makes that share of the trading invisible entirely.
--
-- ★ This layer **knows nothing about launchpads** — any token transfer is recorded, across all three
--   launchpads and both markets. So "which coins this address touched, when and with whom" comes from
--   here, and it is the most complete layer there is.
--
-- Why not query by signer: measured, roughly 25% of buys have a signer who is not the real buyer
--   (delegated signing bots). Three roles: A signs for himself and receives (~75%), B signs for
--   someone else, C has someone else sign and receives. Query 01 covers A+B by signer; this one
--   covers A+C by owner, and only their union is complete.
--
-- ★ amount_usd is always null for meme coins (only 61 of 57,676 tokens have a price feed), **but the
--   quote-token side has it, at the price of that moment** (measured on one address in one day:
--   0.195556 SOL → 19.2759 and 19.2622 USD, implying unit prices of 98.57 and 98.50, which move over
--   time).
--   → USD PnL is the directional sum of amount_usd over **the money legs**. The meme coin itself never
--   needs a price.
with tx as (
    -- ★ All three tables are needed: native SOL + classic SPL + Token-2022.
    --   Native SOL lives in its own sol_transfers table — without it the entire "how much did he pay"
    --   leg disappears.
    select tx_id, block_time, block_slot, tx_index, token_mint_address,
           from_owner, to_owner, amount_display, amount_usd, outer_executing_account
    from tokens_solana.sol_transfers
    where block_date between date '{d0}' and date '{d1}'
      and (to_owner = '{addr}' or from_owner = '{addr}')
    union all
    select tx_id, block_time, block_slot, tx_index, token_mint_address,
           from_owner, to_owner, amount_display, amount_usd, outer_executing_account
    from tokens_solana.spl_token_2022_transfers
    where block_date between date '{d0}' and date '{d1}'
      and (to_owner = '{addr}' or from_owner = '{addr}')
    union all
    select tx_id, block_time, block_slot, tx_index, token_mint_address,
           from_owner, to_owner, amount_display, amount_usd, outer_executing_account
    from tokens_solana.spl_token_transfers
    where block_date between date '{d0}' and date '{d1}'
      and (to_owner = '{addr}' or from_owner = '{addr}')
)
-- ★ Columns cut 2026-09-19: **69-93% of credits is the export cost** (billed on result_set_bytes),
--   and wider rows cost more. Measured on this query at 18,622 rows: 81 credits of export out of 87
--   total.
--   All six removed columns had no reader left downstream (the relationship graph and the delegated-
--   signing logic were dropped along with that part of the product):
--     symbol       09_token_decimals provides it, more completely
--     token_std    explained in the docs, never used in code
--     tx_signer    only filled the signer of a rebuilt fill; measured across three addresses, 100% of
--                  their fills are self-signed
--     self_signed  the delegated-signing recovery was removed
--     from_owner / to_owner  **two 44-character address columns** where downstream only wants "the
--                  counterparty" → composed into a single `peer`
--   direction was already derived from to_owner and is unaffected.
select tx_id, block_time, block_slot, tx_index,
       token_mint_address                      as token,
       if(to_owner = '{addr}', 'IN', 'OUT')    as direction,   -- IN = received tokens (a buy),
                                                               -- OUT = paid tokens (a sell)
       amount_display                          as amt_token,
       amount_usd                              as amt_usd,     -- always null for new meme coins; do not use
       -- ★ The counterparty: the payer when he receives, the payee when he pays. A rebuilt fill uses
       --   it as the pool
       if(to_owner = '{addr}', from_owner, to_owner) as peer,
       coalesce(outer_executing_account, '-')  as router
from tx
order by block_slot, tx_index
-- ⚠️ No row limit. Hit 2026-09-18: with `limit 2000`, a 7-day window filled it exactly → the coin
--    list was truncated → 02 never fetched metadata for those coins → **position coverage fell from
--    335/336 to 296/905 (33%)**. One limit silently discarded two thirds of the data, with no error.
--    The row count is set by the address's activity times the window, not by a number we pick; to
--    control volume, narrow the window.
--    The price: the export cost is billed by bytes, so more rows cost more — but "expensive" is
--    visible and "missing" is not.
