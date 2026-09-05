-- bc_posted_sales_credit_memo_lines: key on the line, not on the item
-- =============================================================================
-- Page 135 standalone was keyed (Document_No, No) - document plus item/account
-- number - on the assumption that a document never repeats an item. BC posts
-- credit memos that do: 27CNBLR-03447 carries Line_No 10000 (14 pcs, 1,135.96)
-- and Line_No 20000 (101 pcs, 12,292.71), both against G/L account 35120010,
-- "SALES RETURN FG". From 2026-09-04 12:47 UTC every run failed on it:
--
--   psycopg.errors.CardinalityViolation: ON CONFLICT DO UPDATE command cannot
--   affect row a second time
--
-- because both rows carry the same key inside one INSERT. Postgres rejects the
-- whole 500-row batch, the run exits 3, and the series watermark cannot
-- advance - so every later run re-fetched the same documents and failed the
-- same way, six runs deep by the time it was looked at.
--
-- Collapsing the duplicates would have made it green and quietly wrong: in the
-- Line_No-grained sibling feed (bc_posted_sales_cr_memo_lines, FY 2026-27) the
-- same document repeats the same account in 8,450 groups covering 19,779
-- lines, so a document+item key keeps 8,450 of them and drops 11,329 - Rs
-- 3,03,03,893 and 499,960 units in one financial year, in a table Birbal
-- reads. So key the table the way its sibling on the same BC page already
-- does: (Document_No, Line_No), the real grain of a posted line.
--
-- Safe on the data as it stands: Line_No is bigint with no nulls, and
-- (Document_No, Line_No) is already unique across all 323,888 stored rows -
-- the collapse dropped the extra lines rather than storing them wrong. The
-- rows lost before this change come back with a --mode full re-pull of this
-- one service; see the README.
--
-- WHY sql/pre/ AND NOT sql/: the ON CONFLICT target must match a real unique
-- constraint. Applying this after the sync, like everything in sql/, would
-- leave one more run failing - and failing differently ("no unique or
-- exclusion constraint matching the ON CONFLICT specification"), because the
-- config would already name the new key. It has to land first.
--
-- Idempotent: does nothing once the primary key is already the new one, which
-- includes a fresh database where ensure_table created it that way.
-- =============================================================================

DO $srgd_cm_key$
DECLARE
    dupes bigint;
    pkdef text;
BEGIN
    IF to_regclass('public.bc_posted_sales_credit_memo_lines') IS NULL THEN
        RETURN;  -- fresh database; the sync creates it with the config key.
    END IF;

    SELECT pg_get_constraintdef(c.oid) INTO pkdef
    FROM pg_constraint c
    WHERE c.conrelid = 'public.bc_posted_sales_credit_memo_lines'::regclass
      AND c.contype = 'p';

    IF pkdef = 'PRIMARY KEY ("Document_No", "Line_No")' THEN
        RETURN;  -- already done.
    END IF;

    -- Refuse rather than fail half way through: ADD PRIMARY KEY would error
    -- on a duplicate anyway, but this names the problem instead of leaving a
    -- table with no primary key at all if somebody ran the DROP by hand.
    SELECT count(*) INTO dupes FROM (
        SELECT 1 FROM public.bc_posted_sales_credit_memo_lines
        GROUP BY "Document_No", "Line_No" HAVING count(*) > 1
    ) d;
    IF dupes > 0 THEN
        RAISE EXCEPTION
            'bc_posted_sales_credit_memo_lines has % duplicate (Document_No, Line_No) pair(s); cannot re-key',
            dupes
        USING HINT = 'Deduplicate them first - keeping the newest _synced_at - then re-run.';
    END IF;

    ALTER TABLE public.bc_posted_sales_credit_memo_lines
        DROP CONSTRAINT bc_posted_sales_credit_memo_lines_pkey;
    -- ADD PRIMARY KEY marks both columns NOT NULL by itself.
    ALTER TABLE public.bc_posted_sales_credit_memo_lines
        ADD CONSTRAINT bc_posted_sales_credit_memo_lines_pkey
        PRIMARY KEY ("Document_No", "Line_No");

    RAISE NOTICE 're-keyed bc_posted_sales_credit_memo_lines from % to (Document_No, Line_No)', pkdef;
END
$srgd_cm_key$;

-- The old key was also the only index on ("Document_No", "No"), and the joins
-- that look a line up by item still want it. Cheap on 324k narrow rows.
CREATE INDEX IF NOT EXISTS bc_posted_sales_credit_memo_lines_doc_no_idx
    ON public.bc_posted_sales_credit_memo_lines ("Document_No", "No");
