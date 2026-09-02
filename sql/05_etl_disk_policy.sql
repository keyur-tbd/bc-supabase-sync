-- Cross-pipeline disk policy.
--
-- WHY: this Supabase volume is shared by several pipelines the same person
-- maintains - the BC sync, the GRN schedulers, and the marketplace/ads
-- loaders. Only the BC sync had a guard, and it watched TOTAL database size,
-- so on 2026-09-02 it would have halted at 25% of its own usage because
-- another pipeline had taken 73%. The pipeline with the smallest footprint
-- was the one that stopped.
--
-- The policy therefore lives in the database, not in any one repo's .env, and
-- the decision logic lives in a FUNCTION so every pipeline behaves identically
-- without duplicating code. A pipeline needs one query:
--
--     SELECT action, reason FROM etl_disk_check('bc_sync');
--     -- action is 'ok' | 'warn' | 'stop'
--
-- A pipeline stops when IT is over its own budget, or when the whole volume
-- is over the ceiling. So the pipeline actually causing the problem is the one
-- that halts, while the others keep running.
--
-- Budgets sum to exactly the global stop threshold (42.5 GB of 50), so the two
-- checks agree rather than contradicting each other.

CREATE TABLE IF NOT EXISTS public.etl_disk_policy (
    pipeline      text PRIMARY KEY,
    table_pattern text[]  NOT NULL DEFAULT '{}',   -- POSIX regex, matched on table name
    budget_gb     numeric,                          -- for '_disk': the VOLUME size
    stop_pct      numeric NOT NULL DEFAULT 100,     -- % of budget_gb at which to stop
    warn_pct      numeric NOT NULL DEFAULT 80,      -- % of budget_gb at which to warn
    enabled       boolean NOT NULL DEFAULT true,
    note          text,
    updated_at    timestamptz NOT NULL DEFAULT now()
);

INSERT INTO public.etl_disk_policy (pipeline, table_pattern, budget_gb, stop_pct, warn_pct, note) VALUES
    ('_disk', '{}', 50, 85, 70,
     'The volume itself. budget_gb = provisioned size; stop at 85%, warn at 70%. Update budget_gb when the volume is resized.'),
    ('bc_sync', '{^bc_,^ref_gst_state$,^etl_}', 12, 100, 80,
     'bc-supabase-sync. 5.85 GB used at 2026-09-02.'),
    ('marketplace', '{^instamart,^zepto,^blinkit,^amz,^fk_,^meta_,^gads_,^mp_}', 24, 100, 80,
     'Ads / marketplace loaders. 15.63 GB used at 2026-09-02 - the largest consumer.'),
    ('grn', '{^nb_,^nbgrn,^nbprn,^hot_,^bb_,^milkbasket,^reliance,^mraws,^doc_,^hyperpure,^flipkart}', 4, 100, 80,
     'GRN schedulers. 0.99 GB used at 2026-09-02.')
ON CONFLICT (pipeline) DO UPDATE
    SET table_pattern = EXCLUDED.table_pattern,
        stop_pct      = EXCLUDED.stop_pct,
        warn_pct      = EXCLUDED.warn_pct,
        note          = EXCLUDED.note,
        updated_at    = now();
        -- budget_gb deliberately NOT overwritten: once set here it is
        -- operational state, and reapplying this file every sync must not
        -- silently undo a deliberate change.

ALTER TABLE public.etl_disk_policy ENABLE ROW LEVEL SECURITY;

-- Bytes currently held by one pipeline's tables.
CREATE OR REPLACE FUNCTION public.etl_pipeline_bytes(p_pipeline text)
RETURNS bigint LANGUAGE sql STABLE AS $$
    SELECT COALESCE(SUM(pg_total_relation_size(c.oid)), 0)::bigint
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    CROSS JOIN LATERAL (
        SELECT table_pattern FROM public.etl_disk_policy WHERE pipeline = p_pipeline
    ) p
    WHERE n.nspname = 'public'
      AND c.relkind IN ('r', 'm')
      AND c.relname ~ ANY (p.table_pattern);
$$;

-- The decision. Every pipeline calls this and obeys the answer.
CREATE OR REPLACE FUNCTION public.etl_disk_check(p_pipeline text)
RETURNS TABLE (
    action        text,
    reason        text,
    used_gb       numeric,
    budget_gb     numeric,
    disk_used_gb  numeric,
    disk_gb       numeric
) LANGUAGE plpgsql STABLE AS $$
DECLARE
    d           record;
    me          record;
    disk_used   numeric := round(pg_database_size(current_database()) / 1024.0^3, 2);
    mine        numeric;
BEGIN
    SELECT * INTO d FROM public.etl_disk_policy WHERE pipeline = '_disk';
    SELECT * INTO me FROM public.etl_disk_policy WHERE pipeline = p_pipeline;

    mine := CASE WHEN me.pipeline IS NULL THEN NULL
                 ELSE round(public.etl_pipeline_bytes(p_pipeline) / 1024.0^3, 2) END;

    -- 1. The volume as a whole. Nobody writes past this, whoever filled it.
    IF d.pipeline IS NOT NULL AND d.enabled
       AND disk_used >= d.budget_gb * d.stop_pct / 100 THEN
        RETURN QUERY SELECT 'stop',
            format('volume at %s GB of %s GB (stop at %s%%) - the whole database is full, regardless of which pipeline filled it',
                   disk_used, d.budget_gb, d.stop_pct),
            mine, me.budget_gb, disk_used, d.budget_gb;
        RETURN;
    END IF;

    -- 2. This pipeline's own budget. Unknown pipelines are governed only by
    --    the volume check above - they are guarded, just not budgeted.
    IF me.pipeline IS NOT NULL AND me.enabled AND me.budget_gb IS NOT NULL
       AND mine >= me.budget_gb * me.stop_pct / 100 THEN
        RETURN QUERY SELECT 'stop',
            format('%s is at %s GB of its %s GB budget', p_pipeline, mine, me.budget_gb),
            mine, me.budget_gb, disk_used, d.budget_gb;
        RETURN;
    END IF;

    IF me.pipeline IS NOT NULL AND me.budget_gb IS NOT NULL
       AND mine >= me.budget_gb * me.warn_pct / 100 THEN
        RETURN QUERY SELECT 'warn',
            format('%s is at %s GB of its %s GB budget (warn at %s%%)',
                   p_pipeline, mine, me.budget_gb, me.warn_pct),
            mine, me.budget_gb, disk_used, d.budget_gb;
        RETURN;
    END IF;

    IF d.pipeline IS NOT NULL AND disk_used >= d.budget_gb * d.warn_pct / 100 THEN
        RETURN QUERY SELECT 'warn',
            format('volume at %s GB of %s GB (warn at %s%%)', disk_used, d.budget_gb, d.warn_pct),
            mine, me.budget_gb, disk_used, d.budget_gb;
        RETURN;
    END IF;

    RETURN QUERY SELECT 'ok',
        format('%s at %s GB of %s GB; volume %s GB of %s GB',
               p_pipeline, COALESCE(mine, 0), COALESCE(me.budget_gb, 0), disk_used, d.budget_gb),
        mine, me.budget_gb, disk_used, d.budget_gb;
END;
$$;
