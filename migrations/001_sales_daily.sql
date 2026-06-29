-- ============================================
-- Migration 001: sales_daily
-- Per-product, per-day sales history.
-- This is the source of truth for the Sales Report and for demand
-- forecasting / par-level suggestions later.
-- Run this in the Supabase SQL Editor before deploying the EOD sync.
-- ============================================

create table if not exists sales_daily (
  id uuid primary key default gen_random_uuid(),
  station_id uuid references stations(id),
  product_id uuid references products(id) on delete set null,
  upc text,
  product_name text,                       -- snapshot, so the report survives renames/removals
  sale_date date not null,
  quantity_sold int not null default 0,
  unit_price decimal(10,2) not null default 0.00,   -- price snapshot at sync time -> revenue = qty * unit_price
  created_at timestamptz default now(),
  unique (product_id, sale_date)           -- one row per product per day; lets the sync upsert idempotently
);

create index if not exists sales_daily_date_idx on sales_daily(sale_date);
create index if not exists sales_daily_station_idx on sales_daily(station_id);

-- ── Row Level Security ──
-- Writes are done by the EOD sync using the service_role key, which bypasses
-- RLS (no insert policy needed). The Sales Report reads via the anon key, so
-- allow public read. (This read grant is part of the surface to revisit in the
-- RLS lockdown / operation #3.)
alter table sales_daily enable row level security;

create policy "Public can read sales_daily"
  on sales_daily for select using (true);
