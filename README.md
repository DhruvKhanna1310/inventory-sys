# QuikPick Inventory System

Barcode scanning inventory tool for QuikPick gas station click-and-collect.

## What This Is

A mobile web app that lets you walk the store, scan product barcodes, auto-lookup product details from Open Food Facts, and save directly to Supabase. No app store, no build step. Open in Safari on your iPhone and go.

## Setup

### 1. Run the Supabase Schema

Go to your Supabase project → SQL Editor → paste the contents of `schema.sql` → Run.

This creates all tables and seeds the pilot station (StoreLocationID 110).

### 2. Get Your Station ID

After running schema.sql, run this query to get the station UUID you will need:

```sql
SELECT id, name FROM stations WHERE passport_store_id = 110;
```

Copy the UUID. This is your `STATION_ID`.

### 3. Deploy index.html

Push to GitHub and enable GitHub Pages, or drag `index.html` to Vercel.

URL will be something like: `https://dhruvkhanna1310.github.io/inventory-sys/`

### 4. Open on iPhone

Open the URL in Safari. Tap Scan. Point camera at any barcode.

## File Structure

```
inventory-sys/
  index.html     ← the complete scanning tool (single file)
  schema.sql     ← run once in Supabase SQL Editor
  README.md
```

## How It Works

1. Tap Scan → camera activates
2. Point at barcode → UPC decoded automatically
3. App checks if UPC already in Supabase (no duplicates)
4. Tries Open Food Facts API → auto-fills name, category, image
5. Falls back to UPC Item DB → fills name and category
6. Falls back to manual entry if both APIs return nothing
7. You enter price and quantity → tap Save
8. Product saved to Supabase products table instantly

## Product Scope (Phase 1)

Scan these:
- Drinks (energy drinks, water, sodas, juice)
- Snacks (chips, candy, jerky, nuts)
- Household basics (Advil, batteries, phone chargers)
- Dairy (milk, eggs if stocked)

Skip these:
- Tobacco (no public UPC API coverage)
- Alcohol (CT licensing)
- Lottery tickets
- PLU items (no barcode)

## Supabase Config

```
URL:  https://dwewijcyvmkhtrjdukqf.supabase.co
Project: quikpick
Store: StoreLocationID 110
```

## Tech

- html5-qrcode for camera barcode scanning
- Open Food Facts API (free, no key)
- UPC Item DB API (100 free lookups/day)
- Supabase JS SDK for database
- Zero dependencies, zero build step
