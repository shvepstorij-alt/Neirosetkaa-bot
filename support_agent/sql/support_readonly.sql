-- Read-only catalog and per-customer price context for the support assistant.
-- Apply once on the shop database as a role that owns these tables.
-- Do not grant the support role INSERT/UPDATE/DELETE. The assistant refuses
-- a credential that can write any public base table.
--
-- The assistant already prefers the live export POST /api/support/catalog for
-- the catalogue. This script is the database path used when
-- SHOP_READONLY_DATABASE_URL is set:
--   * public.support_catalog_readonly
--   * public.support_price_context_readonly(uid, at)
-- Orders stay a direct SELECT on fk_orders restricted by user_id.
--
-- The function returns only markup and promo percents. It does not return
-- partner ids, our discount, margins, emails, passwords, sessions or codes.
-- A missing partner is markup 0 / promo 0, which keeps the public catalogue
-- price. It is not a statement that the client has no orders.

CREATE OR REPLACE VIEW public.support_catalog_readonly AS
SELECT key,
       plan_idx,
       service_name,
       plan_name,
       price,
       sort_order
FROM public.bot_shop_items
WHERE COALESCE(enabled, TRUE) = TRUE
  AND plan_idx >= 0
  AND price > 0;

CREATE OR REPLACE FUNCTION public.support_price_context_readonly(p_uid bigint, p_at timestamp)
RETURNS TABLE (service_key text, markup_pct double precision, promo_pct double precision)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public
AS $$
    WITH partner AS (
        SELECT p.user_id AS partner_id,
               COALESCE(p.partner_markup_pct, 0) AS markup_pct,
               COALESCE(p.partner_promo_pct, 0) AS promo_pct,
               lower(COALESCE(p.partner_promo_mode, 'off')) AS promo_mode,
               COALESCE(p.partner_promo_days, 0) AS promo_days,
               c.created_at AS joined
        FROM users c
        JOIN users p ON p.user_id = c.partner_id
        WHERE c.user_id = p_uid
          AND COALESCE(p.partner, FALSE) = TRUE
    ),
    promo AS (
        SELECT partner.partner_id,
               partner.markup_pct,
               CASE
                   WHEN partner.promo_pct <= 0 THEN 0
                   WHEN partner.promo_mode = 'first'
                        AND COALESCE((SELECT COUNT(*)
                                      FROM partner_earnings e
                                      WHERE e.partner_id = partner.partner_id
                                        AND e.client_id = p_uid), 0) = 0
                       THEN partner.promo_pct
                   WHEN partner.promo_mode = 'days'
                        AND partner.joined IS NOT NULL
                        AND (partner.promo_days - (p_at::date - partner.joined::date)) > 0
                       THEN partner.promo_pct
                   ELSE 0
               END AS promo_pct
        FROM partner
    )
    SELECT items.key::text AS service_key,
           CASE
               WHEN promo.partner_id IS NULL THEN 0
               ELSE COALESCE(rates.markup_pct, promo.markup_pct, 0)
           END AS markup_pct,
           CASE
               WHEN promo.partner_id IS NULL THEN 0
               ELSE COALESCE(promo.promo_pct, 0)
           END AS promo_pct
    FROM (
        SELECT DISTINCT key
        FROM public.support_catalog_readonly
    ) AS items
    LEFT JOIN promo ON TRUE
    LEFT JOIN partner_rates rates
           ON rates.partner_id = promo.partner_id
          AND rates.svc_key = items.key;
$$;

REVOKE ALL ON FUNCTION public.support_price_context_readonly(bigint, timestamp) FROM PUBLIC;

-- Replace support_reader with the role in SHOP_READONLY_DATABASE_URL, then run:
-- GRANT SELECT ON public.support_catalog_readonly TO support_reader;
-- GRANT EXECUTE ON FUNCTION public.support_price_context_readonly(bigint, timestamp) TO support_reader;
-- GRANT SELECT (num, fk_intid, status, amount_rub, coins_spent, paid_at, last_checked_at,
--               pack, fulfilled_at, order_id, user_id)
--   ON public.fk_orders TO support_reader;
-- Optional activation diagnostics, status flags only (never codes, session_raw,
-- account_email, account_pass or payment_link):
-- GRANT SELECT (order_id, user_id, expires_at) ON public.gpt_pending_activations TO support_reader;
-- GRANT SELECT (order_id, user_id, activating_at) ON public.gpt_pending_activations TO support_reader;
-- GRANT SELECT (order_id, used_by, used_at) ON public.gpt_codes TO support_reader;
-- GRANT SELECT (order_id, user_id, expires_at) ON public.claude_pending_activations TO support_reader;
-- GRANT SELECT (order_id, used_by, used_at) ON public.claude_codes TO support_reader;
-- GRANT SELECT (order_id, user_id, expires_at) ON public.perplexity_pending_activations TO support_reader;
-- GRANT SELECT (order_id, used_by, used_at) ON public.perplexity_codes TO support_reader;
-- GRANT SELECT (fk_order_id, user_id, status) ON public.linkpay_orders TO support_reader;
-- GRANT SELECT (fk_order_id, user_id, status) ON public.nsgifts_orders TO support_reader;
