-- Alerting and auto-budgeting for the shared disk policy (see 05_etl_disk_policy.sql).
--
-- THREE PROBLEMS THIS SOLVES
--
-- 1. Nobody reads logs. A warning at 80% of budget is the one that gives you
--    time to act, and it was going nowhere. Alerts now go to email.
-- 2. Alert spam. The syncs run every 2 hours; without state you would get the
--    same warning twelve times a day and start ignoring it. One email per
--    pipeline per cooldown, and immediately again if things get WORSE.
-- 3. New data nobody budgeted for. A new pipeline's tables match no pattern,
--    so they consume the volume while showing under nobody's budget.
--
-- AUTO-BUDGETING, AND WHY IT DOES NOT DEFEAT THE GUARD
--
-- A budget that silently grows whenever it is hit is not a guard. So
-- etl_disk_autobudget() only ever hands out space that is genuinely
-- UNALLOCATED - the difference between the sum of all budgets and the volume's
-- stop threshold. A pipeline that is legitimately growing takes free headroom
-- automatically instead of being blocked by a number somebody guessed months
-- ago; once the volume is fully allocated, expansion stops and the guard bites
-- exactly as before. It can never raise the total past the stop threshold.

-- ---------------------------------------------------------------- alerting --

CREATE TABLE IF NOT EXISTS public.etl_alert_config (
    id             boolean PRIMARY KEY DEFAULT true CHECK (id),   -- single row
    recipients     text[]  NOT NULL,
    cooldown_hours numeric NOT NULL DEFAULT 12,
    enabled        boolean NOT NULL DEFAULT true,
    note           text
);

INSERT INTO public.etl_alert_config (id, recipients, cooldown_hours, note) VALUES
    (true, ARRAY['birbal@thebakersdozen.in'], 12,
     'Who gets disk alerts, and how often at most. Change recipients here - no repo needs editing.')
ON CONFLICT (id) DO NOTHING;   -- never clobber a deliberate change on reapply

-- One row per pipeline: what we last told them, and when.
CREATE TABLE IF NOT EXISTS public.etl_alert_state (
    pipeline     text PRIMARY KEY,
    level        text NOT NULL,                 -- 'ok' | 'warn' | 'stop'
    last_sent_at timestamptz,
    last_reason  text
);

-- Should we email about this pipeline right now? True when the situation is
-- new, has got worse, or the cooldown has expired while still not ok.
CREATE OR REPLACE FUNCTION public.etl_should_alert(p_pipeline text, p_level text)
RETURNS boolean LANGUAGE plpgsql AS $$
DECLARE
    prev  record;
    cfg   record;
    rank  int := CASE p_level WHEN 'stop' THEN 2 WHEN 'warn' THEN 1 ELSE 0 END;
    prank int;
BEGIN
    SELECT * INTO cfg FROM public.etl_alert_config WHERE id;
    IF cfg IS NULL OR NOT cfg.enabled OR rank = 0 THEN
        RETURN false;                    -- alerting off, or nothing wrong
    END IF;
    SELECT * INTO prev FROM public.etl_alert_state WHERE pipeline = p_pipeline;
    IF prev IS NULL THEN
        RETURN true;                     -- never alerted about this one
    END IF;
    prank := CASE prev.level WHEN 'stop' THEN 2 WHEN 'warn' THEN 1 ELSE 0 END;
    IF rank > prank THEN
        RETURN true;                     -- got worse: tell them straight away
    END IF;
    RETURN prev.last_sent_at IS NULL
        OR prev.last_sent_at < now() - make_interval(hours => cfg.cooldown_hours::int);
END;
$$;

CREATE OR REPLACE FUNCTION public.etl_record_alert(p_pipeline text, p_level text, p_reason text, p_sent boolean)
RETURNS void LANGUAGE sql AS $$
    INSERT INTO public.etl_alert_state (pipeline, level, last_sent_at, last_reason)
    VALUES (p_pipeline, p_level, CASE WHEN p_sent THEN now() END, p_reason)
    ON CONFLICT (pipeline) DO UPDATE
        SET level        = EXCLUDED.level,
            last_reason  = EXCLUDED.last_reason,
            -- keep the old timestamp when we did not actually send, so the
            -- cooldown measures time since the last EMAIL, not since the last check
            last_sent_at = COALESCE(EXCLUDED.last_sent_at, public.etl_alert_state.last_sent_at);
$$;

-- ------------------------------------------------------ unbudgeted tables --

-- Tables above p_min_gb that no pipeline pattern claims. These are guarded by
-- the volume ceiling but belong to nobody, so nobody gets warned about them.
CREATE OR REPLACE FUNCTION public.etl_unbudgeted_tables(p_min_gb numeric DEFAULT 0.5)
RETURNS TABLE (table_name text, gb numeric) LANGUAGE sql STABLE AS $$
    SELECT c.relname::text,
           round(pg_total_relation_size(c.oid) / 1024.0^3, 2)
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public'
      AND c.relkind IN ('r', 'm')
      AND pg_total_relation_size(c.oid) >= p_min_gb * 1024.0^3
      AND NOT EXISTS (
            SELECT 1 FROM public.etl_disk_policy p
            WHERE p.pipeline <> '_disk' AND c.relname ~ ANY (p.table_pattern))
    ORDER BY 2 DESC;
$$;

-- ---------------------------------------------------------- auto-budgeting --

-- Grows budgets into UNALLOCATED volume space only. Returns what it changed,
-- so the caller can report it. Never exceeds the volume's stop threshold.
-- DROP first: CREATE OR REPLACE cannot change a function's OUT columns, so
-- editing this signature later fails confusingly without it.
DROP FUNCTION IF EXISTS public.etl_disk_autobudget();
CREATE FUNCTION public.etl_disk_autobudget()
-- NB: the OUT column is pipeline_name, not pipeline - an OUT parameter called
-- `pipeline` shadows the column of that name inside the function body and every
-- reference becomes ambiguous.
RETURNS TABLE (pipeline_name text, old_budget_gb numeric, new_budget_gb numeric, reason text)
LANGUAGE plpgsql AS $$
DECLARE
    d           record;
    r           record;
    ceiling_gb  numeric;
    allocated   numeric;
    free_gb     numeric;
    want        numeric;
    grant_gb    numeric;
BEGIN
    SELECT * INTO d FROM public.etl_disk_policy WHERE pipeline = '_disk';
    IF d IS NULL OR NOT d.enabled THEN RETURN; END IF;

    ceiling_gb := d.budget_gb * d.stop_pct / 100;

    FOR r IN
        SELECT p.pipeline AS name, p.budget_gb, p.warn_pct,
               round(public.etl_pipeline_bytes(p.pipeline) / 1024.0^3, 2) AS used
        FROM public.etl_disk_policy p
        WHERE p.pipeline <> '_disk' AND p.enabled AND p.budget_gb IS NOT NULL
        ORDER BY 1
    LOOP
        CONTINUE WHEN r.used < r.budget_gb * r.warn_pct / 100;   -- not close yet

        SELECT COALESCE(SUM(budget_gb), 0) INTO allocated
        FROM public.etl_disk_policy WHERE pipeline <> '_disk' AND enabled;
        free_gb := ceiling_gb - allocated;
        CONTINUE WHEN free_gb <= 0.5;                            -- nothing to give

        -- Aim for usage + 25% headroom, but only as far as free space allows.
        want     := ceil((r.used * 1.25)::numeric);
        grant_gb := LEAST(want - r.budget_gb, free_gb);
        CONTINUE WHEN grant_gb <= 0;

        UPDATE public.etl_disk_policy
           SET budget_gb = r.budget_gb + grant_gb, updated_at = now()
         WHERE public.etl_disk_policy.pipeline = r.name;

        pipeline_name := r.name;
        old_budget_gb := r.budget_gb;
        new_budget_gb := r.budget_gb + grant_gb;
        reason := format('%s reached %s GB of %s GB; %s GB of the volume was unallocated, so the budget grew to %s GB',
                         r.name, r.used, r.budget_gb, free_gb, new_budget_gb);
        RETURN NEXT;
    END LOOP;
END;
$$;

ALTER TABLE public.etl_alert_config ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.etl_alert_state  ENABLE ROW LEVEL SECURITY;
