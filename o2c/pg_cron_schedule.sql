-- o2c/pg_cron_schedule.sql
-- public.o2c_refresh() runs on the DATABASE's clock (pg_cron), at 09:00 and 14:00 IST.
--
-- WHY NOT GITHUB. .github/workflows/o2c_refresh.yml scheduled it at 09:00 and 18:00 IST,
-- and GitHub started those runs 4-5 HOURS LATE every day (checked 2026-09-11: the 09:00
-- run started 13:44-13:50 IST on three days running, the 18:00 run at 22:06-22:19).
-- Scheduled Actions are best-effort; a workflow_dispatch starts within seconds, which is
-- why the workflow stays for running it by hand. Keyur asked for the afternoon run at
-- 2 PM, replacing the 6 PM one; pg_cron fires on the minute.
--
-- o2c_refresh() rebuilds the o2c bridge, feed_inventory, cogs_refresh() (the costed
-- register, the MIS view, the P&L lines and the Spends board's facts) and the RTV capping
-- ledger -- 12-22 minutes. The job raises its own statement_timeout to 50 min: the server
-- default is 30 and a full spend-facts rebuild can push a run past that.
--
-- pg_cron's clock is GMT: 03:30 = 09:00 IST, 08:30 = 14:00 IST. The Vercel crons that
-- follow this beat (Birbal vercel.json: fill-rate snapshot, sales facts) are timed off
-- these two times -- move them together.
--
-- Apply as postgres, whole file, idempotent. Check runs with:
--   select jobid, status, return_message, start_time, end_time
--     from cron.job_run_details order by start_time desc limit 10;

create extension if not exists pg_cron;

select cron.unschedule(jobname)
  from cron.job
 where jobname in ('o2c-refresh-0900-ist', 'o2c-refresh-1400-ist', 'cron-history-trim');

select cron.schedule('o2c-refresh-0900-ist', '30 3 * * *',
  $$set statement_timeout = '3000000'; select public.o2c_refresh()$$);

select cron.schedule('o2c-refresh-1400-ist', '30 8 * * *',
  $$set statement_timeout = '3000000'; select public.o2c_refresh()$$);

-- pg_cron keeps every run's row forever; a month is plenty to diagnose from
select cron.schedule('cron-history-trim', '15 0 * * *',
  $$delete from cron.job_run_details where end_time < now() - interval '30 days'$$);
