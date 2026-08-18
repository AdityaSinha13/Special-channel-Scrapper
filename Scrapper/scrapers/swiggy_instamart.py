"""
Swiggy Instamart product scraper — refactored from Aditya's original standalone
script into a reusable module. Extraction logic unchanged (4-layer fallback:
__NEXT_DATA__ -> JSON-LD -> meta tags -> live DOM).

Public API mirrors scrapers/blinkit.py:
    ALL_COLS, DEFAULT_FIELDS, extract_id(raw), scrape(ids, cfg, progress_cb, stop_flag)
    cfg = {"location": "122017", "headless": False, "delay_sec": 3.5}
"""
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException
from bs4 import BeautifulSoup
from time import sleep
import json as _json, re, time, random

from .common import make_chrome_driver

ALL_COLS = [
    "PID", "Product Name", "Brand", "Category",
    "Selling Price (Rs)", "MRP (Rs)", "Discount %",
    "Quantity / Size", "Availability", "Delivery Time",
    "Rating", "Rating Count", "Description",
    "Product Image URL", "Scraping Status", "Error Message", "Source URL",
]

DEFAULT_FIELDS = {
    "PID": True, "Product Name": True, "Brand": True, "Category": True,
    "Selling Price (Rs)": True, "MRP (Rs)": True, "Discount %": True,
    "Quantity / Size": True, "Availability": True, "Delivery Time": False,
    "Rating": False, "Rating Count": False, "Description": False,
    "Product Image URL": False, "Scraping Status": True, "Error Message": False,
    "Source URL": False,
}


def extract_id(raw: str) -> str:
    v = str(raw).strip()
    m = re.search(r"/item/[^?/]*--([a-zA-Z0-9]+)", v)
    if m:
        return m.group(1)
    m = re.search(r"/prid/([a-zA-Z0-9\-]+)", v)
    if m:
        return m.group(1)
    m = re.search(r"itemId=([a-zA-Z0-9\-]+)", v)
    if m:
        return m.group(1)
    m = re.search(r"swiggy\.com/instamart/(?:item|product)/([a-zA-Z0-9\-]+)/?$", v)
    if m:
        return m.group(1)
    return v


def set_location(driver, location_text):
    driver.get("https://www.swiggy.com/instamart")
    sleep(5)

    click_xpaths = [
        '//div[@data-testid="search-location"]',
        '//div[contains(@class,"nav-location")]',
        '//div[contains(@class,"location-tab")]',
        '//span[contains(@class,"localize")]',
        '//div[contains(@class,"global-nav")]//div[@role="button"][1]',
        '//div[contains(text(),"Add a new address") or contains(text(),"Enter location")]',
    ]
    for xp in click_xpaths:
        try:
            WebDriverWait(driver, 4).until(EC.element_to_be_clickable((By.XPATH, xp))).click()
            sleep(2)
            break
        except Exception:
            pass

    input_xpaths = [
        '//input[contains(@placeholder,"Search for area") or contains(@placeholder,"search") or contains(@placeholder,"location")]',
        '//input[@id="location-search-input"]',
        '//input[@type="text"][not(@readonly)][1]',
    ]
    for xp in input_xpaths:
        try:
            box = WebDriverWait(driver, 8).until(EC.element_to_be_clickable((By.XPATH, xp)))
            box.clear()
            box.send_keys(str(location_text))
            sleep(3)

            result_xpaths = [
                '//div[contains(@class,"icon-location-marker")]',
                '//li[contains(@class,"location-item")][1]',
                '//div[@role="option"][1]',
                '//ul/li[1]',
                '//div[contains(@class,"PlaceSuggest")][1]',
                '//div[contains(@class,"suggestion")][1]',
            ]
            clicked = False
            for rxp in result_xpaths:
                try:
                    WebDriverWait(driver, 5).until(EC.element_to_be_clickable((By.XPATH, rxp))).click()
                    sleep(2)
                    clicked = True
                    break
                except Exception:
                    pass
            if not clicked:
                box.send_keys(Keys.RETURN)
                sleep(2)

            for cxp in [
                '//button/span[contains(text(),"Confirm")]/..',
                '//button[contains(text(),"Confirm location")]',
                '//button[contains(text(),"Confirm")]',
            ]:
                try:
                    WebDriverWait(driver, 5).until(EC.element_to_be_clickable((By.XPATH, cxp))).click()
                    sleep(2)
                    break
                except Exception:
                    pass

            sleep(3)
            return True

        except TimeoutException:
            continue

    return False


def _clean_price(text):
    """Extract clean numeric price string from messy text like '₹199', 'Rs. 45.50'"""
    if not text:
        return ""
    c = re.sub(r"[^\d.]", "", str(text).replace(",", ""))
    parts = c.split(".")
    if len(parts) > 2:
        c = parts[0] + "." + "".join(parts[1:])
    return c if c else ""


# BUG FIX 5: Recursion depth limit — prevents stack overflow on deep/circular JSON
def _deep(obj, key, _depth=0):
    """Recursively search for a key in nested dict/list structures."""
    if _depth > 20:
        return None
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            r = _deep(v, key, _depth + 1)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for i in obj:
            r = _deep(i, key, _depth + 1)
            if r is not None:
                return r
    return None


def _safe_text(driver, xpaths):
    """Try multiple XPaths, return first non-empty text found."""
    for xp in xpaths:
        try:
            t = driver.find_element(By.XPATH, xp).text.strip()
            if t:
                return t
        except Exception:
            pass
    return ""


def _safe_attr(driver, xpaths, attr="src"):
    """Try multiple XPaths, return first non-empty attribute value found."""
    for xp in xpaths:
        try:
            v = driver.find_element(By.XPATH, xp).get_attribute(attr)
            if v:
                return v.strip()
        except Exception:
            pass
    return ""


# BUG FIX 8: Scroll page to trigger lazy-loaded content before scraping
def _scroll_and_wait(driver, delay=1.0):
    try:
        driver.execute_script("window.scrollTo(0, 400);")
        sleep(delay)
        driver.execute_script("window.scrollTo(0, 0);")
        sleep(0.5)
    except Exception:
        pass


# ── Extraction Method 1: __NEXT_DATA__ JSON ───────────────────────────────────
# BUG FIX 6: Broader key walking — original pageProps.product path doesn't exist
#            in Swiggy's current Next.js structure. Now tries 10 candidate paths.
def parse_next_data(soup):
    out = {}
    tag = soup.find("script", id="__NEXT_DATA__")
    if not tag:
        return out
    try:
        nd = _json.loads(tag.string or "")
        pp = nd.get("props", {}).get("pageProps", {})

        product = None
        candidate_fns = [
            lambda: pp.get("product"),
            lambda: pp.get("item"),
            lambda: pp.get("pdp"),
            lambda: pp.get("itemDetails"),
            lambda: _deep(pp, "itemDetails"),
            lambda: _deep(pp, "product"),
            lambda: _deep(pp, "catalogItem"),
            lambda: _deep(nd, "item"),
            lambda: ((_deep(pp, "store") or {}).get("catalogItems") or [{}])[0],
            lambda: _deep(pp, "itemData"),
        ]
        for fn in candidate_fns:
            try:
                result = fn()
                if result and isinstance(result, dict) and result.get("name"):
                    product = result
                    break
            except Exception:
                pass

        if product:
            out["Product Name"] = str(
                product.get("name") or product.get("display_name") or product.get("itemName") or ""
            )
            out["Brand"] = str(
                product.get("brand") or product.get("brand_name") or product.get("brandName") or ""
            )
            out["Category"] = str(
                product.get("category") or product.get("category_name") or product.get("categoryName") or ""
            )

            # Handle paise vs rupees: if value is round and > 1000, divide by 100
            def maybe_paise(v):
                try:
                    f = float(v)
                    return str(round(f / 100, 2)) if f > 1000 and f % 100 == 0 else str(f)
                except Exception:
                    return str(v)

            raw_mrp = product.get("mrp") or product.get("market_price") or product.get("marketPrice") or ""
            raw_sp  = product.get("price") or product.get("selling_price") or product.get("finalPrice") or ""
            out["MRP (Rs)"]           = _clean_price(maybe_paise(raw_mrp))
            out["Selling Price (Rs)"] = _clean_price(maybe_paise(raw_sp))
            out["Quantity / Size"] = str(
                product.get("unit") or product.get("quantity") or
                product.get("weight") or product.get("net_quantity") or
                product.get("variantTag") or ""
            )

            instock = product.get("inStock")
            oos     = product.get("out_of_stock") or product.get("is_sold_out") or product.get("isSoldOut")
            out["Availability"] = "Out of Stock" if (instock is False or oos) else "In Stock"

            img = (
                product.get("image_url") or product.get("image") or
                product.get("thumbnail") or product.get("imageUrl") or ""
            )
            if isinstance(img, dict):
                img = img.get("url") or img.get("src") or ""
            out["Product Image URL"] = str(img)

            out["Rating"]       = str(product.get("avg_rating") or product.get("rating") or product.get("avgRating") or "")
            out["Rating Count"] = str(product.get("rating_count") or product.get("review_count") or product.get("ratingCount") or "")
            out["Description"]  = str(product.get("description") or product.get("item_description") or product.get("itemDescription") or "")
            out["Delivery Time"] = str(_deep(nd, "sla") or _deep(nd, "eta") or _deep(nd, "deliveryTime") or "")

    except Exception:
        pass
    return {k: v for k, v in out.items() if v}


# ── Extraction Method 2: JSON-LD Schema ───────────────────────────────────────
def parse_json_ld(soup):
    out = {}
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = _json.loads(tag.string or "")
            if not isinstance(data, dict):
                continue
            t = data.get("@type", "")
            if t not in ("Product", "ItemPage") and "Product" not in str(t):
                continue
            out["Product Name"]       = out.get("Product Name") or data.get("name", "")
            brand = data.get("brand") or {}
            out["Brand"]              = out.get("Brand") or (brand.get("name") if isinstance(brand, dict) else str(brand))
            out["Category"]           = out.get("Category") or data.get("category", "")
            imgs = data.get("image", [])
            if isinstance(imgs, str):
                imgs = [imgs]
            out["Product Image URL"]  = out.get("Product Image URL") or (imgs[0] if imgs else "")
            ar = data.get("aggregateRating") or {}
            out["Rating"]             = out.get("Rating") or str(ar.get("ratingValue", ""))
            out["Rating Count"]       = out.get("Rating Count") or str(ar.get("reviewCount") or ar.get("ratingCount", ""))
            offers = data.get("offers") or {}
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            out["Selling Price (Rs)"] = out.get("Selling Price (Rs)") or _clean_price(str(offers.get("price", "")))
            avail = offers.get("availability", "")
            if avail:
                out["Availability"] = "In Stock" if "InStock" in avail else "Out of Stock"
            out["Description"]        = out.get("Description") or data.get("description", "")
        except Exception:
            pass
    return {k: v for k, v in out.items() if v}


# ── Extraction Method 3: OG / Meta Tags ───────────────────────────────────────
def parse_meta(soup):
    out = {}

    def m(prop, name=None):
        tag = soup.find("meta", property=prop) or (
            soup.find("meta", attrs={"name": name}) if name else None
        )
        return (tag.get("content") or "").strip() if tag else ""

    title = m("og:title") or m(None, "title")
    if title:
        out["Product Name"] = re.split(
            r"\s*[|\-–—]\s*(Swiggy|Instamart)", title, flags=re.I
        )[0].strip()
    out["Product Image URL"] = m("og:image")
    desc = m("og:description") or m(None, "description")
    if desc:
        out["Description"] = desc
        pm = re.search(r"₹\s*([\d,]+(?:\.\d+)?)", desc)
        if pm:
            out["Selling Price (Rs)"] = pm.group(1).replace(",", "")
    return {k: v for k, v in out.items() if v}


# ── Extraction Method 4: Live DOM XPath Fallback ──────────────────────────────
def parse_dom(driver):
    out = {}
    out["Product Name"] = _safe_text(driver, [
        "//h1",
        '//*[@data-testid="product_name"]',
        '//*[contains(@class,"ProductName") or contains(@class,"product-name") or contains(@class,"itemName")]',
    ])
    out["Selling Price (Rs)"] = _clean_price(_safe_text(driver, [
        '//*[@data-testid="product_price"]',
        '//*[contains(@class,"finalPrice") or contains(@class,"selling-price")]',
        '//*[contains(@class,"Price") and not(contains(@class,"Mrp")) and not(contains(@style,"line-through"))]',
        '//span[contains(text(),"₹")][not(ancestor::*[contains(@style,"line-through")])][1]',
    ]))
    out["MRP (Rs)"] = _clean_price(_safe_text(driver, [
        '//*[@data-testid="product_mrp"]',
        '//*[contains(@class,"Mrp") or contains(@class,"mrp")]',
        '//*[contains(@style,"line-through")]',
        '//span[contains(@class,"strike") or contains(@class,"crossed")]',
    ]))
    out["Quantity / Size"] = _safe_text(driver, [
        '//*[@data-testid="product_weight"]',
        '//*[contains(@class,"Weight") or contains(@class,"weight")]',
        '//*[contains(@class,"Quantity") or contains(@class,"variantTag")]',
    ])
    out["Brand"] = _safe_text(driver, [
        '//*[contains(@class,"BrandName") or contains(@class,"brand-name") or contains(@class,"brandName")]',
        '//a[contains(@href,"/brand")]',
        '//*[@data-testid="brand_name"]',
    ])
    out["Description"] = _safe_text(driver, [
        '//*[@data-testid="product_description"]',
        '//*[contains(@class,"Description") or contains(@class,"description")]',
    ])
    oos_text = _safe_text(driver, [
        '//*[contains(@class,"SoldOut") or contains(@class,"sold-out") or contains(@class,"outOfStock")]',
        '//button[contains(text(),"Sold") or contains(text(),"Out of Stock") or contains(text(),"Notify")]',
    ])
    out["Availability"] = "Out of Stock" if oos_text else "In Stock"
    out["Delivery Time"] = _safe_text(driver, [
        '//*[contains(@class,"sla") or contains(@class,"delivery-time") or contains(@class,"Eta")]',
        '//span[contains(text(),"min") and not(contains(text(),"ago"))]',
    ])
    out["Rating"]       = _safe_text(driver, ['//*[contains(@class,"Rating")]//span[1]'])
    out["Rating Count"] = _safe_text(driver, ['//*[contains(@class,"RatingCount") or contains(@class,"review-count")]'])
    try:
        crumbs = driver.find_elements(
            By.XPATH, '//nav[contains(@class,"breadcrumb")]//a | //ol//li//a'
        )
        if crumbs:
            out["Category"] = " > ".join(c.text.strip() for c in crumbs if c.text.strip())
    except Exception:
        pass
    out["Product Image URL"] = _safe_attr(driver, [
        '//*[@data-testid="product_image"]//img',
        '//*[contains(@class,"ProductImage") or contains(@class,"product-image")]//img',
        '//main//img[not(contains(@src,"svg"))][1]',
    ], "src")
    return {k: v for k, v in out.items() if v}


# ── Master Extractor ──────────────────────────────────────────────────────────
def extract_product(driver, pid):
    """Run all 4 extraction methods in order, filling gaps. Returns full data dict."""
    result = {
        "PID": pid, "Product Name": "", "Brand": "", "Category": "",
        "Selling Price (Rs)": "", "MRP (Rs)": "", "Discount %": "",
        "Quantity / Size": "", "Availability": "", "Delivery Time": "",
        "Rating": "", "Rating Count": "", "Description": "",
        "Product Image URL": "", "Scraping Status": "Failed",
        "Error Message": "", "Source URL": driver.current_url,
    }
    try:
        _scroll_and_wait(driver, delay=1.0)  # BUG FIX 8: trigger lazy-load
        soup = BeautifulSoup(driver.page_source, "html.parser")

        # Method 1 & 2 — structured data (most reliable)
        for fn in [parse_next_data, parse_json_ld]:
            d = fn(soup)
            result.update({k: v for k, v in d.items() if v and not result.get(k)})

        # Method 3 — meta tags (fill remaining gaps)
        if not result.get("Product Name"):
            d = parse_meta(soup)
            result.update({k: v for k, v in d.items() if v and not result.get(k)})

        # Method 4 — live DOM (last resort)
        if not result.get("Product Name") or not result.get("Selling Price (Rs)"):
            d = parse_dom(driver)
            result.update({k: v for k, v in d.items() if v and not result.get(k)})

        # Auto-calculate discount % if we have both prices
        if not result.get("Discount %") and result.get("MRP (Rs)") and result.get("Selling Price (Rs)"):
            try:
                mrp = float(result["MRP (Rs)"])
                sp  = float(result["Selling Price (Rs)"])
                if mrp > 0 and sp > 0 and mrp >= sp:
                    result["Discount %"] = str(round((mrp - sp) / mrp * 100, 1)) + "%"
            except Exception:
                pass

        result["Scraping Status"] = (
            "Success"
            if (result.get("Product Name") or result.get("Selling Price (Rs)"))
            else "Failed"
        )
        if result["Scraping Status"] == "Failed":
            result["Error Message"] = "No data found — check PID/URL or Swiggy page structure"

    except Exception as e:
        result["Scraping Status"] = "Failed"
        result["Error Message"]   = str(e)

    return result


# ── File Loader ───────────────────────────────────────────────────────────────
# BUG FIX 4 & 9: Fixed URL parsing for Instamart (old code used /prid/ — Swiggy Food pattern!)
# Supports:
#   1. Bare PIDs:         12345  /  ABC-xyz
#   2. Instamart URLs:    https://www.swiggy.com/instamart/item/dettol-hand-wash--12345
#   3. itemId param URLs: ...?itemId=12345
#   4. Old food URLs:     .../prid/12345  (kept for backwards compat)


# ═══════════════════════════════════════════════════════════════
#  DRIVER-DRIVEN RUN LOOP  (replaces the original script's main())
# ═══════════════════════════════════════════════════════════════
def scrape(pids, cfg, progress_cb=None, stop_flag=None):
    """
    pids       : list[str] raw PIDs or full Instamart URLs
    cfg        : {"location": str, "headless": bool, "delay_sec": float}
    progress_cb: optional callable(idx, total, row_dict)
    stop_flag  : optional common.StopFlag()
    Returns list[dict] with columns from ALL_COLS.
    """
    location = cfg.get("location", "122017")
    headless = cfg.get("headless", False)
    delay_sec = cfg.get("delay_sec", 3.5)

    ids = [extract_id(p) for p in pids]
    results = []
    driver = None
    try:
        driver = make_chrome_driver(headless=headless)
        set_location(driver, location)

        for idx, pid in enumerate(ids, 1):
            if stop_flag and stop_flag.stop:
                break
            try:
                url = f"https://www.swiggy.com/instamart/item/{pid}"
                driver.get(url)
                try:
                    WebDriverWait(driver, 15).until(
                        EC.presence_of_element_located((By.XPATH, "//h1 | //main"))
                    )
                except TimeoutException:
                    pass
                sleep(delay_sec + random.uniform(0.5, 1.5))

                current = driver.current_url
                page_src = driver.page_source
                redirected = (
                    "/instamart/item/" not in current
                    and "/instamart/product/" not in current
                    and "instamart" in current
                )
                if redirected or len(page_src) < 5000:
                    url2 = f"https://www.swiggy.com/instamart/product-detail?itemId={pid}"
                    driver.get(url2)
                    sleep(delay_sec)

                data = extract_product(driver, pid)
                data["Source URL"] = driver.current_url

            except Exception as e:
                data = {col: "" for col in ALL_COLS}
                data["PID"] = pid
                data["Scraping Status"] = "Failed"
                data["Error Message"] = str(e)[:150]
                data["Source URL"] = driver.current_url if driver else ""

            results.append(data)
            if progress_cb:
                progress_cb(idx, len(ids), data)

            if idx < len(ids):
                sleep(delay_sec + random.uniform(0, 1.2))

    finally:
        if driver:
            driver.quit()

    return results
