import asyncio
import csv
import re
import sys
import argparse
import sqlite3
import json
from datetime import datetime
from pathlib import Path
from urllib.parse import quote
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

DB_PATH = Path(__file__).parent / "athena.db"

def _db():
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = _db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS searches (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        query TEXT NOT NULL,
        category TEXT,
        location TEXT,
        max_results INTEGER,
        lead_count INTEGER,
        created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS leads (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        search_id INTEGER,
        dedup_key TEXT UNIQUE,
        name TEXT,
        phone TEXT,
        phone_norm TEXT,
        website TEXT,
        has_website TEXT,
        address TEXT,
        hours TEXT,
        rating TEXT,
        reviews TEXT,
        maps_url TEXT,
        category TEXT,
        query TEXT,
        created_at TEXT,
        FOREIGN KEY(search_id) REFERENCES searches(id)
    );
    CREATE INDEX IF NOT EXISTS idx_leads_category ON leads(category);
    CREATE INDEX IF NOT EXISTS idx_leads_phone_norm ON leads(phone_norm);
    """)
    conn.commit()
    conn.close()

def normalize_phone(p):
    if not p: return ""
    d = re.sub(r'\D','',p)
    return d[-10:] if len(d)>=10 else d

def dedup_key_for(r):
    pn = normalize_phone(r.get("phone",""))
    if pn: return f"ph:{pn}"
    mu = (r.get("maps_url") or "").strip().lower()
    if mu: return f"url:{mu}"
    return f"name:{(r.get('name','')+'|'+r.get('address','')).strip().lower()}"

def extract_category_location(query):
    q = query.strip()
    lower = q.lower()
    cat, loc = q, ""
    m = re.match(r'^(.*?)\s+(?:in|at)\s+(.+)$', lower)
    if m:
        cat = q[:m.start(2)-4].strip() if " in " in lower else q[:m.start(2)-3].strip()
        loc = q[m.start(2):].strip()
        # better: split original case
        parts = re.split(r'\s+(?:in|at)\s+', q, flags=re.I)
        if len(parts)==2:
            cat, loc = parts[0].strip(), parts[1].strip()
    else:
        # take first 2 words as category
        cat = q.split(",")[0].strip()
    return cat[:80], loc[:120]

def db_save_search(query, max_results, leads):
    try:
        init_db()
        cat, loc = extract_category_location(query)
        conn = _db()
        now = datetime.utcnow().isoformat()
        cur = conn.execute("INSERT INTO searches (query, category, location, max_results, lead_count, created_at) VALUES (?,?,?,?,?,?)", (query, cat, loc, max_results, len(leads), now))
        search_id = cur.lastrowid
        inserted = 0
        skipped = 0
        for r in leads:
            dk = dedup_key_for(r)
            try:
                conn.execute("""INSERT INTO leads (search_id, dedup_key, name, phone, phone_norm, website, has_website, address, hours, rating, reviews, maps_url, category, query, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (search_id, dk, r.get("name",""), r.get("phone",""), normalize_phone(r.get("phone","")), r.get("website",""), r.get("has_website",""), r.get("address",""), r.get("hours",""), r.get("rating",""), r.get("reviews",""), r.get("maps_url",""), cat, query, now))
                inserted += 1
            except sqlite3.IntegrityError:
                skipped += 1
        conn.commit()
        conn.close()
        print(f"[DB] Saved search #{search_id} '{query}' -> {inserted} new, {skipped} dup skipped")
        return search_id, inserted, skipped
    except Exception as e:
        print(f"[DB ERROR] {e}")
        return None, 0, 0

SEARCH_QUERY = "dentists in Koramangala Bengaluru"
MAX_RESULTS = 50
OUTPUT_FILE = "google_maps_leads.csv"
HEADLESS = True
CONCURRENCY = 6
DELAY_SECONDS = 0.4
MAX_SCROLLS = 15
SERVER_HOST = "127.0.0.1"
SERVER_PORT = 8000

async def safe_text(locator):
    try:
        if await locator.count() == 0:
            return ""
        text = await locator.first.inner_text(timeout=3000)
        return text.strip()
    except Exception:
        return ""

async def safe_attribute(locator, attribute):
    try:
        if await locator.count() == 0:
            return ""
        value = await locator.first.get_attribute(attribute, timeout=3000)
        return value.strip() if value else ""
    except Exception:
        return ""

async def scroll_results(page):
    print("\n[*] Scrolling Google Maps results...")
    feed = page.locator('div[role="feed"]')
    if await feed.count() == 0:
        print("[!] Results feed not found")
        return
    previous_count = 0
    unchanged = 0
    for i in range(MAX_SCROLLS):
        try:
            cards = page.locator('a[href*="/maps/place/"]')
            current_count = await cards.count()
            print(f"    Scroll {i + 1}/{MAX_SCROLLS} | businesses found: {current_count}")
            if current_count >= MAX_RESULTS:
                print("[*] Reached target result count, stopping scroll early")
                break
            if current_count == previous_count:
                unchanged += 1
            else:
                unchanged = 0
            previous_count = current_count
            if unchanged >= 3:
                print("[*] No new businesses appearing")
                break
            await feed.evaluate("(el) => { el.scrollTop = el.scrollHeight; }")
            await page.wait_for_timeout(900)
        except Exception as e:
            print(f"[!] Scroll error: {e}")
            break

async def collect_businesses(page):
    print("\n[*] Collecting business listings...")
    links = page.locator('a[href*="/maps/place/"]')
    count = await links.count()
    businesses = []
    seen_urls = set()
    for i in range(count):
        try:
            link = links.nth(i)
            name = await link.get_attribute("aria-label")
            href = await link.get_attribute("href")
            if not name or not href:
                continue
            if href in seen_urls:
                continue
            seen_urls.add(href)
            businesses.append({"name": name.strip(), "url": href})
            if len(businesses) >= MAX_RESULTS:
                break
        except Exception:
            continue
    print(f"[+] Collected {len(businesses)} unique businesses")
    return businesses

async def scrape_business(page, business):
    name = business["name"]
    data = {
        "name": name,
        "phone": "",
        "website": "",
        "has_website": "No",
        "address": "",
        "hours": "",
        "rating": "",
        "reviews": "",
        "maps_url": "",
    }
    try:
        await page.goto(business["url"], wait_until="domcontentloaded", timeout=30000)
        try:
            await page.locator("h1").first.wait_for(timeout=6000)
        except PlaywrightTimeoutError:
            pass
        data["maps_url"] = page.url
        phone_selectors = [
            'button[data-item-id^="phone:"]',
            'button[aria-label^="Phone:"]',
            'a[href^="tel:"]',
        ]
        for selector in phone_selectors:
            locator = page.locator(selector)
            if await locator.count() > 0:
                phone = await safe_attribute(locator, "aria-label")
                if not phone:
                    phone = await safe_text(locator)
                if phone:
                    phone = re.sub(r"^(Phone:\s*)", "", phone, flags=re.I).strip()
                    data["phone"] = phone
                    break
        address_selectors = [
            'button[data-item-id="address"]',
            'button[aria-label^="Address:"]',
        ]
        for selector in address_selectors:
            locator = page.locator(selector)
            if await locator.count() > 0:
                address = await safe_attribute(locator, "aria-label")
                if not address:
                    address = await safe_text(locator)
                if address:
                    address = re.sub(r"^(Address:\s*)", "", address, flags=re.I).strip()
                    data["address"] = address
                    break
        website_selectors = [
            'a[data-item-id="authority"]',
            'a[aria-label^="Website:"]',
        ]
        for selector in website_selectors:
            locator = page.locator(selector)
            if await locator.count() > 0:
                website = await safe_attribute(locator, "href")
                if website:
                    data["website"] = website
                    break
        data["has_website"] = "Yes" if data["website"] else "No"
        try:
            hours = ""
            for sel in ['button[data-item-id="oh"]','div[data-item-id="oh"]','button[aria-label*="Hours"]','[data-item-id*="hours"]']:
                loc = page.locator(sel)
                if await loc.count() > 0:
                    h = await safe_attribute(loc, "aria-label")
                    if not h or "hours" in h.lower():
                        h = await safe_text(loc)
                    if h:
                        h = re.sub(r'^Hours:?\s*', '', h, flags=re.I).strip()
                        if len(h) < 30 or "Monday" not in h:
                            try:
                                await loc.first.click(timeout=1500)
                                await page.wait_for_timeout(700)
                                h2 = await safe_text(page.locator('[data-item-id="oh"]'))
                                if h2 and len(h2) > len(h):
                                    h = h2
                            except Exception:
                                pass
                        hours = h.replace("\n"," | ").strip()[:600]
                        break
            if not hours:
                try:
                    body = await page.locator('div[role="main"]').first.inner_text(timeout=3000)
                    if "Monday" in body or "Open 24" in body:
                        lines = [l.strip() for l in body.split("\n") if l.strip()]
                        hl = [l for l in lines if any(d in l for d in ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday","Open 24","Closed"])]
                        if hl:
                            hours = " | ".join(hl[:14])
                        else:
                            m = re.search(r'(Monday.*?Sunday[^\n]*)', body, re.S|re.I)
                            if m:
                                hours = m.group(1).replace("\n"," | ").strip()
                except Exception:
                    pass
            data["hours"] = hours
        except Exception:
            pass
        try:
            star_loc = page.locator('span[role="img"][aria-label*="star"]')
            if await star_loc.count() > 0:
                aria = await safe_attribute(star_loc, "aria-label")
                if aria:
                    m = re.search(r'([0-5](?:\.\d)?)', aria)
                    if m:
                        data["rating"] = m.group(1)
                    m2 = re.search(r'([\d,]+)\s*reviews?', aria, re.I)
                    if m2:
                        data["reviews"] = m2.group(1)
            if not data["rating"] or not data["reviews"]:
                rating_locator = page.locator('div[role="main"]').first
                body_text = await rating_locator.inner_text(timeout=5000)
                match = re.search(r"([0-5](?:\.\d)?)\s*\(([\d,]+)\)", body_text)
                if match:
                    if not data["rating"]:
                        data["rating"] = match.group(1)
                    if not data["reviews"]:
                        data["reviews"] = match.group(2)
                else:
                    match = re.search(r"([0-5](?:\.\d)?)\s+([\d,]+)\s+reviews", body_text, re.I)
                    if match:
                        if not data["rating"]:
                            data["rating"] = match.group(1)
                        if not data["reviews"]:
                            data["reviews"] = match.group(2)
                    else:
                        m3 = re.search(r'([\d,]+)\s+reviews?', body_text, re.I)
                        if m3 and not data["reviews"]:
                            data["reviews"] = m3.group(1)
        except Exception:
            pass
        print(f"[{name}] phone={data['phone'] or 'NONE'} | website={data['has_website']} | hrs={data['hours'][:40] if data['hours'] else 'NONE'} | rating={data['rating'] or 'NONE'} ({data['reviews'] or '0'} reviews)")
        return data
    except PlaywrightTimeoutError:
        print(f"[{name}] [TIMEOUT]")
        return data
    except Exception as e:
        print(f"[{name}] [ERROR] {e}")
        return data

async def run_worker_pool(context, businesses):
    semaphore = asyncio.Semaphore(CONCURRENCY)
    results = [None] * len(businesses)
    async def worker(index, business):
        async with semaphore:
            page = await context.new_page()
            try:
                result = await scrape_business(page, business)
                results[index] = result
            finally:
                await page.close()
                if DELAY_SECONDS:
                    await asyncio.sleep(DELAY_SECONDS)
    tasks = [worker(i, b) for i, b in enumerate(businesses)]
    await asyncio.gather(*tasks)
    return results

def save_csv(results):
    fields = ["name","phone","website","has_website","address","hours","rating","reviews","maps_url"]
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in results:
            writer.writerow({k: row.get(k,"") for k in fields})
    print(f"\n[+] Saved {len(results)} leads")
    print(f"[+] File: {OUTPUT_FILE}")

async def scrape_leads(query: str, max_results: int = MAX_RESULTS):
    global MAX_RESULTS
    _prev_max = MAX_RESULTS
    MAX_RESULTS = max_results
    try:
        print("=" * 70)
        print("GOOGLE MAPS LEAD SCRAPER (fast, concurrent)")
        print("=" * 70)
        print(f"\nQuery       : {query}")
        print(f"Max results : {max_results}")
        print(f"Concurrency : {CONCURRENCY}")
        print(f"Headless    : {HEADLESS}")
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=HEADLESS,
                args=["--disable-blink-features=AutomationControlled","--disable-dev-shm-usage","--no-sandbox"],
            )
            context = await browser.new_context(
                viewport={"width": 1440, "height": 900},
                locale="en-US",
                timezone_id="Asia/Kolkata",
            )
            page = await context.new_page()
            search_url = "https://www.google.com/maps/search/" + quote(query)
            print("\n[*] Opening:")
            print(search_url)
            try:
                await page.goto(search_url, wait_until="domcontentloaded", timeout=60000)
            except Exception as e:
                print(f"[!] Initial navigation warning: {e}")
            try:
                await page.locator('div[role="feed"]').wait_for(timeout=15000)
            except Exception:
                print("[!] Google Maps results feed did not load.")
                print("[!] Current URL:", page.url)
                await browser.close()
                return []
            await scroll_results(page)
            businesses = await collect_businesses(page)
            if not businesses:
                print("\n[!] No businesses found.")
                await browser.close()
                return []
            print(f"\n[*] Scraping {len(businesses)} businesses with {CONCURRENCY} parallel tabs...")
            results = await run_worker_pool(context, businesses)
            results = [r for r in results if r is not None]
            await browser.close()
            return results
    finally:
        MAX_RESULTS = _prev_max

async def main():
    results = await scrape_leads(SEARCH_QUERY, MAX_RESULTS)
    if results:
        save_csv(results)
        with_phone = sum(1 for x in results if x["phone"])
        with_website = sum(1 for x in results if x["website"])
        print("\n" + "=" * 70)
        print(f"TOTAL LEADS      : {len(results)}")
        print(f"WITH PHONE       : {with_phone}")
        print(f"WITHOUT PHONE    : {len(results) - with_phone}")
        print(f"WITH WEBSITE     : {with_website}")
        print(f"WITHOUT WEBSITE  : {len(results) - with_website}")
        print("=" * 70)

try:
    from fastapi import FastAPI, Query
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import JSONResponse, FileResponse
    from fastapi.staticfiles import StaticFiles
    import uvicorn
    HAS_FASTAPI = True
except ImportError:
    HAS_FASTAPI = False

if HAS_FASTAPI:
    app = FastAPI(title="ATHENA Scraper", version="2.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    # serve frontend + black.gif on same origin for single-service Railway deploy
    try:
        from pathlib import Path as _P
        _here = _P(__file__).parent
        if (_here / "black.gif").exists():
            app.mount("/black.gif", StaticFiles(directory=str(_here), html=False), name="black")
    except Exception:
        pass

    @app.get("/api/health")
    async def health():
        return {"status": "ok", "service": "cere-maps-scraper"}

    @app.get("/api/search")
    async def api_search(q: str = Query(..., description="Search query e.g. 'dentists in Koramangala'"), max_results: int = Query(50, ge=1, le=100), headless: bool = Query(True)):
        global HEADLESS
        prev_headless = HEADLESS
        HEADLESS = headless
        try:
            results = await scrape_leads(q, max_results)
            leads = []
            for r in results:
                leads.append({
                    "id": r.get("maps_url") or r.get("name"),
                    "name": r.get("name",""),
                    "phone": r.get("phone",""),
                    "website": r.get("website",""),
                    "hasWebsite": r.get("has_website") == "Yes",
                    "has_website": r.get("has_website","No"),
                    "address": r.get("address",""),
                    "hours": r.get("hours",""),
                    "rating": r.get("rating",""),
                    "reviews": r.get("reviews",""),
                    "maps_url": r.get("maps_url",""),
                    "status": "",
                })
            db_save_search(q, max_results, results)
            return {"query": q, "count": len(leads), "leads": leads}
        except Exception as e:
            return JSONResponse(status_code=500, content={"error": str(e)})
        finally:
            HEADLESS = prev_headless

    @app.post("/api/search")
    async def api_search_post(payload: dict):
        q = payload.get("q") or payload.get("query") or ""
        if not q:
            return JSONResponse(status_code=400, content={"error": "Missing 'q' / 'query' field"})
        max_results = int(payload.get("max_results", payload.get("maxResults", 50)))
        return await api_search(q, max_results)

    @app.get("/api/history")
    async def api_history(limit: int = Query(100, ge=1, le=500), category: str = Query(None), q: str = Query(None)):
        init_db()
        conn = _db()
        sql = "SELECT * FROM searches"
        params = []
        wh = []
        if category:
            wh.append("category LIKE ?")
            params.append(f"%{category}%")
        if q:
            wh.append("query LIKE ?")
            params.append(f"%{q}%")
        if wh:
            sql += " WHERE " + " AND ".join(wh)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        conn.close()
        return {"history": [dict(r) for r in rows]}

    @app.get("/api/leads")
    async def api_leads(category: str = Query(None), query: str = Query(None), has_website: str = Query(None), has_phone: str = Query(None), limit: int = Query(500, ge=1, le=2000), offset: int = Query(0)):
        init_db()
        conn = _db()
        sql = "SELECT * FROM leads"
        params = []
        wh = []
        if category:
            wh.append("category LIKE ?")
            params.append(f"%{category}%")
        if query:
            wh.append("(query LIKE ? OR name LIKE ?)")
            params.extend([f"%{query}%", f"%{query}%"])
        if has_website == "yes":
            wh.append("has_website='Yes'")
        elif has_website == "no":
            wh.append("has_website='No'")
        if has_phone == "yes":
            wh.append("phone!=''")
        elif has_phone == "no":
            wh.append("phone=''")
        if wh:
            sql += " WHERE " + " AND ".join(wh)
        sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        rows = conn.execute(sql, params).fetchall()
        total = conn.execute("SELECT COUNT(*) as c FROM leads" + (" WHERE " + " AND ".join(wh) if wh else ""), params[:-2] if wh else []).fetchone()["c"] if False else len(rows)
        conn.close()
        # map to frontend shape
        leads = []
        for r in rows:
            d = dict(r)
            leads.append({"id": d["dedup_key"], "name": d["name"], "phone": d["phone"], "website": d["website"], "hasWebsite": d["has_website"]=="Yes", "address": d["address"], "hours": d["hours"], "rating": d["rating"], "reviews": d["reviews"], "maps_url": d["maps_url"], "category": d["category"], "query": d["query"]})
        return {"count": len(leads), "leads": leads}

    @app.get("/api/stats")
    async def api_stats():
        init_db()
        conn = _db()
        total_searches = conn.execute("SELECT COUNT(*) as c FROM searches").fetchone()["c"]
        total_leads = conn.execute("SELECT COUNT(*) as c FROM leads").fetchone()["c"]
        with_phone = conn.execute("SELECT COUNT(*) as c FROM leads WHERE phone!=''").fetchone()["c"]
        without_site = conn.execute("SELECT COUNT(*) as c FROM leads WHERE has_website='No'").fetchone()["c"]
        cats = conn.execute("SELECT category, COUNT(*) as cnt FROM leads GROUP BY category ORDER BY cnt DESC LIMIT 20").fetchall()
        recent = conn.execute("SELECT * FROM searches ORDER BY id DESC LIMIT 5").fetchall()
        conn.close()
        return {"total_searches": total_searches, "total_leads": total_leads, "with_phone": with_phone, "without_website": without_site, "by_category": [dict(r) for r in cats], "recent": [dict(r) for r in recent]}

    @app.delete("/api/history/{search_id}")
    async def api_delete_search(search_id: int):
        init_db()
        conn = _db()
        conn.execute("DELETE FROM searches WHERE id=?", (search_id,))
        conn.commit()
        conn.close()
        return {"ok": True}

    @app.delete("/api/leads/clear")
    async def api_clear_leads():
        init_db()
        conn = _db()
        conn.execute("DELETE FROM leads")
        conn.execute("DELETE FROM searches")
        conn.commit()
        conn.close()
        return {"ok": True}

    @app.get("/")
    async def serve_frontend():
        import pathlib
        p = pathlib.Path(__file__).parent / "maps-lead-finder.html"
        if p.exists():
            return FileResponse(str(p), media_type="text/html")
        return {"service":"ATHENA Scraper","health":"/api/health","search":"/api/search?q=..."} 

    @app.get("/black.gif")
    async def serve_gif():
        import pathlib
        p = pathlib.Path(__file__).parent / "black.gif"
        if p.exists():
            return FileResponse(str(p), media_type="image/gif")
        return JSONResponse(status_code=404, content={"error":"not found"})

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Cere Maps Lead Finder")
    parser.add_argument("--serve", "--server", action="store_true", dest="serve", help="Run as HTTP backend for maps-lead-finder.html")
    parser.add_argument("--host", default=SERVER_HOST, help="Server host")
    parser.add_argument("--port", type=int, default=SERVER_PORT, help="Server port")
    parser.add_argument("--query", "-q", default=None, help="One-off scrape query (CLI mode)")
    parser.add_argument("--max-results", type=int, default=MAX_RESULTS)
    args = parser.parse_args()
    if args.serve:
        if not HAS_FASTAPI:
            print("[!] FastAPI/uvicorn not installed. Run: pip install fastapi uvicorn")
            sys.exit(1)
        print(f"[*] Starting Cere backend at http://{args.host}:{args.port}")
        print(f"[*] Frontend should use: http://{args.host}:{args.port}/api/search?q=YOUR_QUERY")
        print(f"[*] Health check: http://{args.host}:{args.port}/api/health")
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    else:
        if args.query:
            SEARCH_QUERY = args.query
        if args.max_results:
            MAX_RESULTS = args.max_results
        asyncio.run(main())
