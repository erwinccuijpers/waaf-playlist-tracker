-- Paywall: a watch is only tracked once paid. New bot-created watches start as
-- status='pending_payment' with active=false; a successful Telegram payment flips
-- them to status='active', active=true. The daily checker only runs active watches.
alter table public.cw_watches
  add column if not exists status text not null default 'active',
  add column if not exists paid_at timestamptz,
  add column if not exists telegram_payment_charge_id text,
  add column if not exists provider_payment_charge_id text;

comment on column public.cw_watches.status is 'pending_payment | active | expired';
