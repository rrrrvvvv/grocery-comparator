# Grocery price comparator

A no-frills mobile web app for deciding, in the aisle, whether the price tag in front of you is good. It pulls flyer prices for your postal code from Flipp (which aggregates Loblaws / No Frills / Save-On / Sobeys / Walmart Canada / etc.), keeps a rolling history per item, and turns each item into a one-tap **GOOD / FAIR / HIGH** verdict.

```
   ┌────────────────────┐
   │   Grocery          │
   │ ─────────────────  │
   │  Peanut butter 1kg │ GOOD ≤ $5.99
   │  Best $5.49 @ NoFr │
   │ ─────────────────  │
   │  Lean ground beef  │ FAIR ≤ $5.49/lb
   │  Best $4.99 @ Save │
   └────────────────────┘
```

No servers to babysit. The price scraper runs once a day on **GitHub Actions** (free tier), writes `web/prices.json` back into the repo, and the static HTML page is served by **GitHub Pages**. Your phone just opens the URL.

---

## What's in this repo

| Path | Purpose |
|---|---|
| `scraper/scrape.py`        | Python scraper. Hits Flipp, computes thresholds, writes `web/prices.json`. |
| `scraper/items.json`       | **Your shopping list.** Edit here (or from the app). |
| `scraper/price_history.json` | Rolling 90-day best-price history per item — auto-managed. |
| `.github/workflows/scrape.yml` | Daily cron (11:30 UTC). Also runs whenever you change `items.json`. |
| `web/index.html`           | The mobile-first PWA. |
| `web/manifest.webmanifest`, `web/sw.js`, `web/icon-*.png` | PWA bits for "Add to home screen". |
| `web/prices.json`          | Latest scraper output. **Don't hand-edit** — the scraper overwrites it. |

---

## One-time setup (≈10 minutes)

### 1. Create a GitHub repo and push this folder

If you have the GitHub CLI:
```bash
cd "Grocery price comparator"
gh repo create grocery-comparator --public --source=. --remote=origin --push
```
Or do it through the GitHub UI: create a new repo, then:
```bash
git init
git add .
git commit -m "initial"
git branch -M main
git remote add origin https://github.com/<your-username>/grocery-comparator.git
git push -u origin main
```

### 2. Tell the scraper your postal code

In your repo on GitHub: **Settings → Secrets and variables → Actions → Variables → New repository variable**

| Name | Value |
|---|---|
| `POSTAL_CODE` | your postal code, no spaces (e.g. `V5K0A1`) |

> Why a *variable* and not a secret? Postal code isn't sensitive, and variables are slightly easier to edit later. If you'd rather hide it, use a Secret with the same name — the workflow checks both.

### 3. Run the scraper once, manually

In your repo: **Actions tab → "scrape-prices" → Run workflow → Run**.

Wait ~30 seconds. When it finishes, `web/prices.json` will be populated, and a commit titled `scrape: refresh prices …` will appear on `main`.

### 4. Turn on GitHub Pages

In your repo: **Settings → Pages**.
- **Source**: *Deploy from a branch*
- **Branch**: `main`, folder `/web` (or `/ (root)` and rename `web` → `docs` if Pages doesn't offer `/web`)
- Click **Save**.

After ~1 minute Pages will give you a URL like `https://<your-username>.github.io/grocery-comparator/`. Open it on your phone.

### 5. Add to home screen

In Chrome on Android, tap the ⋮ menu → **Add to Home screen** → name it "Grocery". Done — tap the icon to open instantly.

### 6. (Optional) Enable in-app list editing

By default, the app reads your list. To **edit** it from your phone (add/remove items), give it a tiny GitHub permission:

1. On GitHub: **Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token**.
2. **Resource owner**: you. **Repository access**: only this repo. **Permissions → Repository permissions → Contents: Read and write**.
3. Copy the token (starts with `github_pat_…`).
4. In the app, tap the gear icon → paste the token → Save.

The app will commit `scraper/items.json` directly when you add/edit/remove items. The Action runs on push, so prices refresh within a couple of minutes.

> The token never leaves your phone — it lives in `localStorage` only. If you ever need to revoke it, do so on the same GitHub settings page.

---

## How verdicts work

For each item, the scraper records the **best price across all merchants** every day, keeps the last 90 days, and computes:

- **GOOD** — current price is at or below the **25th percentile** of that history (a real deal — what the price drops to during sales).
- **FAIR** — at or below the **median** (typical sale price).
- **HIGH** — anything else.

If you'd rather hard-set a target ("anything under $5 is good"), set `good_price` on the item — it overrides the auto-computed threshold.

For the first ~2 weeks there isn't enough history to compute meaningful thresholds, so the app shows the current best price without a verdict band. After that the bands settle in.

---

## Adding & editing items

Two ways:

**From the app** (needs PAT, see step 6 above): tap **+ Add item** at the bottom, or tap **✎ Edit item** inside an expanded row.

**From the repo**: edit `scraper/items.json` directly. Fields per item:

| Field | Required | Notes |
|---|---|---|
| `id` | yes | unique slug; used as a stable key |
| `name` | yes | what shows on your phone |
| `query` | yes | what gets sent to Flipp's search |
| `category` | optional | groups items in the UI (Produce, Meat & Poultry, Dairy & Eggs, Bakery, Pantry, Frozen, Beverages, Snacks). Anything else falls under *Other* |
| `type` | yes | `"specific"` or `"generic"` (only affects how matches are filtered) |
| `unit_size` | optional | `{"value": 1, "unit": "kg"}` — enables unit-price display. Weight units (`g`, `kg`, `lb`, `oz`) render as **$/100g · $/lb**. Volume units (`mL`, `L`) render as **$/100mL · $/L**. Count (`ea`) renders as **$/ea** |
| `good_price` | optional | hard target — overrides auto thresholds |
| `match.must_include` | optional | list of substrings the offer name **must** contain (case-insensitive) |
| `match.any_of` | optional | offer name must contain **at least one** of these (e.g. `"1 kg"`, `"1kg"`, `"1000 g"`) |
| `match.exclude` | optional | offer is dropped if name contains any of these |

Pushing to `main` (or the in-app save) automatically triggers a scrape.

---

## Diagnostics & troubleshooting

**"No current sale" everywhere on first load.**
You haven't run the scraper yet — go to Actions and click *Run workflow* once.

**Some items have no offers but you know they're on sale.**
The query is too narrow. Try a shorter, more generic `query` — or add an `any_of` match clause to broaden the size variants.

**Wrong items matching (e.g. dog food showing up under "peanut butter").**
Add to `match.exclude` (e.g. `"dog"`).

**Prices feel stale.**
Cron runs at 11:30 UTC daily. To force a refresh, run the workflow manually or commit any change.

**The scraper crashed because Flipp changed their API.**
Open `scraper/scrape.py`. The function `offer_from_raw` is the only place that depends on Flipp's response shape — adjust the field names there. The scrape job log on GitHub Actions will show the failing item & response excerpt.

**I want a different region.**
Change `POSTAL_CODE` in repo variables. Flipp's coverage scales with how dense your area is — major Canadian cities have 5+ chains; smaller towns may be 1–2.

---

## Why Flipp?

Most Canadian chain grocers don't expose stable price APIs, but they all submit weekly flyers to Flipp/Reebee/Wishabi (now consolidated under Flipp). Their public flyer search endpoint returns enough structured data — name, current price, merchant, sale story, validity dates — to power exactly this kind of list. We hit it once a day at a polite rate.

This is for personal grocery shopping. Don't redistribute the data, don't hammer the endpoint, and respect Flipp's TOS.

---

## License

Personal use. Do whatever you want with the code; no warranty.
