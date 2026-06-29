-- ============================================
-- QuikPick Database Schema
-- StoreLocationID: 110
-- Run this in Supabase SQL Editor
-- ============================================

-- STATIONS
create table stations (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  address text not null,
  passport_store_id int default 110,
  lat float,
  lng float,
  phone text,
  is_active boolean default true,
  commission_rate decimal(4,2) default 0.00,
  created_at timestamptz default now()
);

-- PRODUCTS
create table products (
  id uuid primary key default gen_random_uuid(),
  station_id uuid references stations(id),
  upc text,
  passport_item_id text,
  plu_code text,
  name text not null,
  passport_description text,
  price decimal(10,2) not null default 0.00,
  category text default 'other',
  merchandise_code int,
  stock_quantity int not null default 0,
  buffer_threshold int not null default 2,
  is_active boolean default true,
  image_url text,
  last_synced_at timestamptz,
  manual_override boolean default false,
  created_at timestamptz default now()
);

-- ORDERS
create table orders (
  id uuid primary key default gen_random_uuid(),
  station_id uuid references stations(id),
  customer_name text not null,
  customer_phone text not null,
  customer_email text,
  total_amount decimal(10,2) not null,
  commission_amount decimal(10,2) default 0.00,
  status text default 'placed',
  pickup_code text unique,
  stripe_payment_id text,
  created_at timestamptz default now(),
  confirmed_at timestamptz,
  ready_at timestamptz,
  picked_up_at timestamptz
);

-- ORDER ITEMS
create table order_items (
  id uuid primary key default gen_random_uuid(),
  order_id uuid references orders(id),
  product_id uuid references products(id),
  product_name text not null,
  unit_price decimal(10,2) not null,
  quantity int not null default 1,
  upc text
);

-- STATION STAFF
create table station_staff (
  id uuid primary key default gen_random_uuid(),
  station_id uuid references stations(id),
  email text unique not null,
  role text default 'staff',
  created_at timestamptz default now()
);

-- INVENTORY SYNC LOG
create table inventory_sync_log (
  id uuid primary key default gen_random_uuid(),
  station_id uuid references stations(id),
  sync_date date not null,
  items_processed int default 0,
  items_matched int default 0,
  items_unmatched int default 0,
  items_flagged_low int default 0,
  sync_status text default 'success',
  error_log text,
  duration_seconds int,
  created_at timestamptz default now()
);

-- ============================================
-- SEED: Pilot Station (StoreLocationID 110)
-- ============================================
insert into stations (name, address, passport_store_id, phone, is_active, commission_rate)
values (
  'QuikPick Pilot — State St',
  'State Street, Hamden, CT',
  110,
  '+1 (203) 410-4994',
  true,
  0.00
);

-- ============================================
-- ROW LEVEL SECURITY
-- ============================================
alter table stations enable row level security;
alter table products enable row level security;
alter table orders enable row level security;
alter table order_items enable row level security;
alter table station_staff enable row level security;
alter table inventory_sync_log enable row level security;

-- Allow public read on stations and products (for customer app)
create policy "Public can read active stations"
  on stations for select using (is_active = true);

create policy "Public can read active products"
  on products for select using (is_active = true);

-- Allow full access for anon key (scanning tool in Phase 1)
-- Tighten these in Phase 2 when staff auth is added
create policy "Anon can insert products"
  on products for insert with check (true);

create policy "Anon can update products"
  on products for update using (true);

create policy "Anon can read all products"
  on products for select using (true);

-- ============================================
-- INDEX for fast UPC lookup
-- ============================================
create index products_upc_idx on products(upc);
create index products_station_idx on products(station_id);
