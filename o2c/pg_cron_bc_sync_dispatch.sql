-- o2c/pg_cron_bc_sync_dispatch.sql
-- The BC sync (.github/workflows/bc_sync.yml) is STARTED by the database's clock (pg_cron + pg_net), not by GitHub's.
--
-- WHY. The workflow's own schedule ("58 */2 * * *", every 2 hours) was honoured about four times a day: on 2026-10-28
-- runs started 07:31 and 14:51 IST and nothing in between, so invoice 27GUR-06779 (e-invoiced 10:54 IST) reached the
-- database at 15:15 and missed both o2c beats. Scheduled Actions are best-effort; a workflow_dispatch starts in
-- seconds (same reason o2c_refresh moved to pg_cron, see pg_cron_schedule.sql). Keyur, 2026-10-28: "move the sync to
-- pg_cron".
--
-- WHEN (IST): 06:00, 08:00, 10:00, 13:00, 15:00, 17:00, 19:00, 21:00, 23:00, 01:00, 03:00. A run takes 35-50 min, so
-- the 08:00 and 13:00 runs land before the 09:00 / 14:00 o2c beats; the others are followed by Birbal's
-- public.birbal_scm_follow_up() (Birbal migration 225), which refreshes the bridge once a sync has finished.
-- A tick is skipped while a sync is still running (etl_sync_state 'in_progress' < 3 h old): GitHub would only queue
-- a second run behind it.
--
-- TOKEN. Vault secret 'github_bc_sync_dispatch': a fine-grained token of keyur-tbd limited to this repository with
-- Actions: Read and write -- nothing else. Rotate with
--   select vault.update_secret((select id from vault.secrets where name = 'github_bc_sync_dispatch'), '<token>');
-- An expired or revoked token shows as http_status 401 in public.bc_sync_dispatch_log; the workflow keeps a
-- twice-daily GitHub schedule as a fallback so the sync never stops altogether.
--
-- Apply as postgres, whole file, idempotent (hand-applied like pg_cron_schedule.sql -- never via apply_sql.py).
-- Check with:
--   select * from public.bc_sync_dispatch_log order by id desc limit 10;

create extension if not exists pg_net with schema extensions;

create table if not exists public.bc_sync_dispatch_log (
  id             bigserial primary key,
  requested_at   timestamptz not null default now(),
  skipped_reason text,
  net_request_id bigint,
  http_status    integer,
  response       text
);
alter table public.bc_sync_dispatch_log enable row level security;
revoke all on public.bc_sync_dispatch_log from anon, authenticated;
comment on table public.bc_sync_dispatch_log is
  'One row per pg_cron tick of bc-sync-dispatch (o2c/pg_cron_bc_sync_dispatch.sql): the GitHub workflow_dispatch request, '
  'or why it was skipped. http_status 204 = GitHub accepted it; 401/403/404 = the vault token is wrong or expired.';

create or replace function public.bc_sync_dispatch()
  returns jsonb
  language plpgsql
  security definer
  set search_path = pg_catalog, public, pg_temp
as $function$
declare
  v_busy  text;
  v_token text;
  v_id    bigint;
begin
  -- earlier requests: pg_net answers asynchronously and keeps responses for 6 hours
  update public.bc_sync_dispatch_log l
     set http_status = r.status_code,
         response = left(coalesce(nullif(r.content, ''), r.error_msg, case when r.timed_out then 'timed out' end), 500)
    from net._http_response r
   where r.id = l.net_request_id and l.http_status is null;

  select string_agg(s.service_name, ', ') into v_busy
    from public.etl_sync_state s
   where s.status = 'in_progress' and s.last_run_started_at > now() - interval '3 hours';
  if v_busy is not null then
    insert into public.bc_sync_dispatch_log (skipped_reason) values ('a sync is running: ' || v_busy);
    return jsonb_build_object('dispatched', false, 'reason', 'a sync is running', 'services', v_busy);
  end if;

  select ds.decrypted_secret into v_token from vault.decrypted_secrets ds where ds.name = 'github_bc_sync_dispatch';
  if v_token is null then
    insert into public.bc_sync_dispatch_log (skipped_reason) values ('no github_bc_sync_dispatch secret in the vault');
    return jsonb_build_object('dispatched', false, 'reason', 'no token');
  end if;

  v_id := net.http_post(
    url := 'https://api.github.com/repos/keyur-tbd/bc-supabase-sync/actions/workflows/bc_sync.yml/dispatches',
    body := jsonb_build_object('ref', 'main'),
    headers := jsonb_build_object('Authorization', 'Bearer ' || v_token,
                                  'Accept', 'application/vnd.github+json',
                                  'X-GitHub-Api-Version', '2022-11-28',
                                  'User-Agent', 'supabase-pg-cron-bc-sync',
                                  'Content-Type', 'application/json'),
    timeout_milliseconds := 15000);
  insert into public.bc_sync_dispatch_log (net_request_id) values (v_id);
  return jsonb_build_object('dispatched', true, 'net_request_id', v_id);
end
$function$;
revoke execute on function public.bc_sync_dispatch() from public, anon, authenticated;

select cron.unschedule(jobname) from cron.job where jobname = 'bc-sync-dispatch';
-- pg_cron's clock is GMT: 00:30 = 06:00 IST ... 21:30 = 03:00 IST
select cron.schedule('bc-sync-dispatch', '30 0,2,4,7,9,11,13,15,17,19,21 * * *',
  $$select public.bc_sync_dispatch()$$);
