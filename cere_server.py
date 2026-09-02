import asyncio
import csv
import io
import re
from urllib.parse import quote
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

app = FastAPI(title="Cere Maps Lead Finder API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

HEADLESS = True
CONCURRENCY = 6
DELAY_SECONDS = 0.4
MAX_SCROLLS = 15
MAX_RESULTS_DEFAULT = 50

class SearchRequest(BaseModel):
    query: str
    maxResults: Optional[int] = MAX_RESULTS_DEFAULT
    headless: Optional[bool] = True

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

async def scroll_results(page, max_results):
    feed = page.locator('div[role="feed"]')
    if await feed.count() == 0:
        return
    previous_count = 0
    unchanged = 0
    for i in range(MAX_SCROLLS):
        try:
            cards = page.locator('a[href*="/maps/place/"]')
            current_count = await cards.count()
            if current_count >= max_results:
                break
            if current_count == previous_count:
                unchanged += 1
            else:
                unchanged = 0
            previous_count = current_count
            if unchanged >= 3:
                break
            await feed.evaluate("(el) => { el.scrollTop = el.scrollHeight; }")
            await page.wait_for_timeout(900)
        except Exception:
            break

async def collect_businesses(page, max_results):
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
            if len(businesses) >= max_results:
                break
        except Exception:
            continue
    return businesses

async def scrape_business(page, business):
    name = business["name"]
    data = {
        "id": business["url"],
        "name": name,
        "phone": "",
        "website": "",
        "hasWebsite": False,
        "address": "",
        "hours": "",
        "rating": "",
        "reviews": "",
        "status": "",
        "maps_url": business["url"],
    }
    try:
        await page.goto(business["url"], wait_until="domcontentloaded", timeout=30000)
        try:
            await page.locator("h1").first.wait_for(timeout=6000)
        except PlaywrightTimeoutError:
            pass
        data["maps_url"] = page.url
        for selector in ['button[data-item-id^="phone:"]','button[aria-label^="Phone:"]','a[href^="tel:"]']:
            locator = page.locator(selector)
            if await locator.count() > 0:
                phone = await safe_attribute(locator, "aria-label")
                if not phone:
                    phone = await safe_text(locator)
                if phone:
                    phone = re.sub(r"^(Phone:\s*)", "", phone, flags=re.I).strip()
                    data["phone"] = phone
                    break
        for selector in ['button[data-item-id="address"]','button[aria-label^="Address:"]']:
            locator = page.locator(selector)
            if await locator.count() > 0:
                address = await safe_attribute(locator, "aria-label")
                if not address:
                    address = await safe_text(locator)
                if address:
                    address = re.sub(r"^(Address:\s*)", "", address, flags=re.I).strip()
                    data["address"] = address
                    break
        for selector in ['a[data-item-id="authority"]','a[aria-label^="Website:"]']:
            locator = page.locator(selector)
            if await locator.count() > 0:
                website = await safe_attribute(locator, "href")
                if website:
                    data["website"] = website
                    break
        data["hasWebsite"] = bool(data["website"])
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
        except Exception:
            pass
        return data
    except PlaywrightTimeoutError:
        return data
    except Exception:
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
    return [r for r in results if r is not None]

async def scrape_maps(query: str, max_results: int, headless: bool):
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=headless,
            args=["--disable-blink-features=AutomationControlled","--disable-dev-shm-usage","--no-sandbox"],
        )
        context = await browser.new_context(
            viewport={"width": 1440, "height": 900},
            locale="en-US",
            timezone_id="Asia/Kolkata",
        )
        page = await context.new_page()
        search_url = "https://www.google.com/maps/search/" + quote(query)
        try:
            await page.goto(search_url, wait_until="domcontentloaded", timeout=60000)
        except Exception:
            pass
        try:
            await page.locator('div[role="feed"]').wait_for(timeout=15000)
        except Exception:
            await browser.close()
            raise HTTPException(status_code=502, detail="Google Maps results did not load (maybe blocked / no results)")
        await scroll_results(page, max_results)
        businesses = await collect_businesses(page, max_results)
        if not businesses:
            await browser.close()
            return []
        results = await run_worker_pool(context, businesses)
        await browser.close()
        return results

@app.get("/health")
@app.get("/api/health")
async def health():
    return {"status": "ok"}

@app.get("/api/search")
async def api_search_get(q: str = "", query: str = "", max_results: int = 50, maxResults: Optional[int] = None):
    actual_query = (q or query).strip()
    if not actual_query:
        raise HTTPException(status_code=400, detail="query is required (q or query)")
    mr = maxResults if maxResults is not None else max_results
    mr = max(1, min(mr, 100))
    leads = await scrape_maps(actual_query, mr, HEADLESS)
    return {"query": actual_query, "count": len(leads), "leads": leads}

@app.post("/api/search")
async def api_search(req: SearchRequest):
    query = req.query.strip()
    if not query:
        raise HTTPException(status_code=400, detail="query is required")
    max_results = max(1, min(req.maxResults or MAX_RESULTS_DEFAULT, 100))
    try:
        leads = await scrape_maps(query, max_results, req.headless if req.headless is not None else HEADLESS)
        return {"query": query, "count": len(leads), "leads": leads}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/search/csv")
async def api_search_csv(req: SearchRequest):
    query = req.query.strip()
    if not query:
        raise HTTPException(status_code=400, detail="query is required")
    max_results = max(1, min(req.maxResults or MAX_RESULTS_DEFAULT, 100))
    leads = await scrape_maps(query, max_results, req.headless if req.headless is not None else HEADLESS)
    fields = ["name","phone","website","hasWebsite","address","rating","reviews","hours","maps_url"]
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    for r in leads:
        writer.writerow({k: r.get(k,"") for k in fields})
    csv_bytes = "\ufeff" + output.getvalue()
    safe = re.sub(r"[^a-z0-9]+","-", query.lower()).strip("-") or "leads"
    return StreamingResponse(
        io.BytesIO(csv_bytes.encode("utf-8")),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{safe}-leads.csv"'}
    )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
