"""
Flipkart FSN scraper — refactored from Aditya's original notebook (v12,
JSON-LD based) into a reusable module.

IMPORTANT — same constraint as the original notebook: Flipkart gates real
pricing behind login + a delivery pincode set inside the browser session, so
this module needs a **visible, human-assisted** browser. The Streamlit app
therefore runs this in two phases:
    1. open_driver()               -> launches Chrome, opens flipkart.com
       (user logs in + sets pincode manually in that window)
    2. scrape(driver, fsns, ...)   -> once the user confirms "ready", scrape
       each FSN's product page using the already-authenticated session.

Extraction is JSON-first (schema.org <script type="application/ld+json">,
then window.__INITIAL_STATE__), with text-pattern fallback only when both
are missing — this is what survives Flipkart's frequent CSS class churn.

Public API:
    ALL_COLS, DEFAULT_FIELDS, extract_id(raw)
    open_driver(headless=False) -> driver
    scrape(driver, fsns, selected_fields, progress_cb=None, stop_flag=None) -> list[dict]
"""
import re
import time
import random
import json
from datetime import datetime

from bs4 import BeautifulSoup
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from .common import make_chrome_driver

ALL_COLS = [
    "FSN", "Product Name", "Brand", "Category", "Selling Price (Rs)", "MRP (Rs)",
    "Discount", "Rating", "Rating Count", "Review Count", "Pack Size", "Seller",
    "Seller Rating", "Availability", "Highlights", "Image URL",
    "Scraped At", "Status",
]

DEFAULT_FIELDS = {
    "Product Name": True, "Brand": True, "Category": True,
    "Selling Price (Rs)": True, "MRP (Rs)": False, "Discount": False,
    "Rating": False, "Rating Count": False, "Review Count": False,
    "Pack Size": True, "Seller": True, "Seller Rating": False,
    "Availability": False, "Highlights": False, "Image URL": False,
}

DELAY_MIN = 3.0
DELAY_MAX = 5.0


def extract_id(raw: str) -> str:
    """Normalise a pasted Flipkart URL or bare FSN into an FSN."""
    v = str(raw).strip()
    m = re.search(r"[?&]pid=([A-Z0-9]+)", v, re.I)
    if m:
        return m.group(1).upper()
    return v.upper()


def open_driver(headless: bool = False):
    """Launch Chrome and land on flipkart.com so the user can log in and set
    their delivery pincode by hand before scraping starts."""
    driver = make_chrome_driver(headless=headless, window_size="1920,1080")
    driver.get("https://www.flipkart.com")
    time.sleep(2)
    close_popup(driver)
    return driver


def close_popup(driver):
    try:
        driver.find_element(By.XPATH, '//button[contains(@class,"_2KpZ6l")]').click()
        time.sleep(0.4)
    except Exception:
        pass


def load_page(driver, url):
    """Load page, scroll so dynamic sections render, return BeautifulSoup."""
    try:
        driver.get(url)
        WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        close_popup(driver)
        for y in [600, 1500, 2500, 4000]:
            driver.execute_script(f"window.scrollTo(0, {y});")
            time.sleep(0.5)
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(1.0)
        driver.execute_script("window.scrollTo(0, 0);")
        time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))
        return BeautifulSoup(driver.page_source, "lxml")
    except Exception:
        return None


def gt(el):
    return el.get_text(strip=True) if el else ""


def clean_price(text):
    if text is None:
        return ""
    n = re.sub(r"[^\d.]", "", str(text))
    try:
        return n if n and float(n) > 0 else ""
    except ValueError:
        return ""


def safe_get(d, *path, default=None):
    """Walk a nested dict/list safely: safe_get(data, 'offers', 0, 'price')"""
    cur = d
    for key in path:
        try:
            if isinstance(key, int):
                cur = cur[key]
            else:
                cur = cur.get(key)
        except (KeyError, IndexError, TypeError, AttributeError):
            return default
        if cur is None:
            return default
    return cur


def extract_jsonld(soup):
    """Returns the schema.org Product dict from the ld+json script tag, or {}."""
    for tag in soup.find_all("script", type="application/ld+json"):
        raw = tag.string or tag.text
        if not raw or '"@type":"Product"' not in raw.replace(" ", ""):
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if isinstance(item, dict) and item.get("@type") == "Product":
                return item
    return {}


def extract_initial_state(soup):
    """Pulls the big __INITIAL_STATE__ = {...}; JSON blob from inline <script>."""
    for tag in soup.find_all("script"):
        txt = tag.string or tag.text
        if not txt or "__INITIAL_STATE__" not in txt:
            continue
        m = re.search(r"__INITIAL_STATE__\s*=\s*(\{.*?\});", txt, re.S)
        if not m:
            continue
        raw = m.group(1)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            for end in range(len(raw), max(len(raw) - 5000, 0), -1):
                try:
                    return json.loads(raw[:end])
                except json.JSONDecodeError:
                    continue
    return {}


def parse_product_name(soup, ld, state):
    name = ld.get('name')
    if name and len(name) > 3:
        return name.strip()
    # fallback: <h1> on page (full text, not truncated)
    h1 = soup.find('h1')
    if h1:
        t = gt(h1)
        if len(t) > 3:
            return t
    og = soup.find('meta', property='og:title')
    if og and og.get('content'):
        t = re.sub(r'\s*[-|]\s*Flipkart.*', '', og['content']).strip()
        if len(t) > 3:
            return t
    return 'N/A'

def parse_brand(soup, ld, state):
    brand = safe_get(ld, 'brand', 'name')
    if brand:
        return brand.strip()
    # fallback: specs table row labelled Brand
    for row in soup.select('tr'):
        cols = row.select('td')
        if len(cols) >= 2:
            key = gt(cols[0]).lower()
            if key in ('brand', 'manufacturer', 'brand name'):
                val = gt(cols[1])
                if val:
                    return val
    return 'N/A'

def parse_category(soup, ld, state):
    cat = ld.get('category')
    if cat:
        return cat.strip()
    # fallback: breadcrumb links
    crumbs = [gt(a) for a in soup.select('a') if gt(a)]
    return 'N/A'

def parse_selling_price(soup, ld, state):
    p = safe_get(ld, 'offers', 'price')
    if p not in (None, ''):
        cp = clean_price(p)
        if cp:
            return cp
    # fallback: any element whose text starts with rupee sign and looks like a price
    for el in soup.find_all(['div', 'span']):
        t = gt(el)
        if t.startswith('\u20b9') and 2 < len(t) < 12:
            cp = clean_price(t)
            if cp:
                return cp
    return 'N/A'

def parse_mrp(soup, ld, state):
    # JSON-LD doesn't carry MRP directly; look for strikethrough text near price
    for el in soup.find_all(['div', 'span']):
        style = el.get('style', '') or ''
        cls = ' '.join(el.get('class', []) or [])
        if 'line-through' in style or 'line-through' in cls or 'text-decoration-line:line-through' in style:
            t = gt(el)
            if '\u20b9' in t:
                cp = clean_price(t)
                if cp:
                    return cp
    for el in soup.find_all(['s', 'del', 'strike']):
        t = gt(el)
        if '\u20b9' in t:
            cp = clean_price(t)
            if cp:
                return cp
    return 'N/A'

def parse_discount(soup, ld, state):
    # Look for "NN% off" / "NN% OFF" pattern text anywhere on page
    text_blob = soup.get_text(' ', strip=True)
    m = re.search(r'(\d{1,2})\s*%\s*off', text_blob, re.I)
    if m:
        return f'{m.group(1)}% off'
    # else compute from price + mrp
    sp = parse_selling_price(soup, ld, state)
    mrp = parse_mrp(soup, ld, state)
    if sp not in ('N/A', '') and mrp not in ('N/A', ''):
        try:
            s, mv = float(sp), float(mrp)
            if mv > s > 0:
                return f'{round((mv - s) / mv * 100)}% off'
        except (ValueError, ZeroDivisionError):
            pass
    return 'N/A'

def parse_rating(soup, ld, state):
    r = safe_get(ld, 'aggregateRating', 'ratingValue')
    if r not in (None, ''):
        return str(r)
    return 'N/A'

def parse_rating_count(soup, ld, state):
    c = safe_get(ld, 'aggregateRating', 'ratingCount')
    if c not in (None, ''):
        return str(c)
    return 'N/A'

def parse_review_count(soup, ld, state):
    c = safe_get(ld, 'aggregateRating', 'reviewCount')
    if c not in (None, ''):
        return str(c)
    return 'N/A'

def parse_pack_size(soup, ld, state):
    # Look in specs table for "Pack of" attribute
    for row in soup.select('tr'):
        cols = row.select('td')
        if len(cols) >= 2:
            key = gt(cols[0]).lower()
            if 'pack' in key:
                val = gt(cols[1])
                if val:
                    return val
    text_blob = soup.get_text(' ', strip=True)
    m = re.search(r'pack\s*of\s*(\d+)', text_blob, re.I)
    if m:
        return f'Pack of {m.group(1)}'
    return 'N/A'

def parse_seller(soup, ld, state, driver=None):
    text_blob = soup.get_text(' ', strip=True)
    # Stop at the seller-name boundary (max 4 words) so trailing rating
    # numbers like "4.1" or discount text don't get swallowed into the name.
    m = re.search(
        r'(?:Sold|Fulfilled|Dispatched)\s+by\s+'
        r'([A-Za-z][A-Za-z0-9&.\-]*(?:\s+[A-Za-z][A-Za-z0-9&.\-]*){0,3})',
        text_blob
    )
    if m:
        return m.group(1).strip()
    # live DOM fallback via selenium text search (in case soup missed it)
    if driver is not None:
        try:
            el = driver.find_element(By.XPATH, '//*[contains(text(),"Fulfilled by") or contains(text(),"Sold by")]')
            t = el.text.strip()
            m2 = re.search(
                r'(?:Sold|Fulfilled|Dispatched)\s+by\s+'
                r'([A-Za-z][A-Za-z0-9&.\-]*(?:\s+[A-Za-z][A-Za-z0-9&.\-]*){0,3})',
                t
            )
            if m2:
                return m2.group(1).strip()
        except Exception:
            pass
    return 'N/A'

def parse_seller_rating(soup, ld, state):
    text_blob = soup.get_text(' ', strip=True)
    # Look for a rating number close to seller name context e.g. "EKKAART 4.1"
    m = re.search(r'(?:Sold|Fulfilled|Dispatched)\s+by\s+[A-Za-z0-9 .&\-]{2,60}[^\d]{0,15}(\d\.\d)', text_blob, re.I)
    if m:
        return m.group(1)
    return 'N/A'

def parse_availability(soup, ld, state):
    avail = safe_get(ld, 'offers', 'availability', default='')
    if avail:
        return 'In Stock' if 'instock' in avail.lower() else 'Out of Stock'
    pg = soup.get_text(' ', strip=True).lower()
    if any(x in pg for x in ['out of stock', 'currently unavailable', 'notify me', 'sold out']):
        return 'Out of Stock'
    return 'In Stock'

def parse_highlights(soup, ld, state):
    # Specs table rows -> "Key: Value" joined by " | "
    pairs = []
    for row in soup.select('tr'):
        cols = row.select('td')
        if len(cols) >= 2:
            key = gt(cols[0])
            val = gt(cols[1])
            if key and val:
                pairs.append(f'{key}: {val}')
    if pairs:
        return ' | '.join(pairs[:15])  # cap length so Excel cell stays usable
    return 'N/A'

def parse_image_url(soup, ld, state):
    img = ld.get('image')
    if isinstance(img, list) and img:
        return img[0]
    if isinstance(img, str) and img:
        return img
    og = soup.find('meta', property='og:image')
    if og and og.get('content'):
        return og['content']
    return 'N/A'

# ── Master per-FSN scraper ─────────────────────────────────────
PARSERS = {
    'Product Name'       : lambda s, ld, st, d=None: parse_product_name(s, ld, st),
    'Brand'              : lambda s, ld, st, d=None: parse_brand(s, ld, st),
    'Category'           : lambda s, ld, st, d=None: parse_category(s, ld, st),
    'Selling Price (Rs)' : lambda s, ld, st, d=None: parse_selling_price(s, ld, st),
    'MRP (Rs)'           : lambda s, ld, st, d=None: parse_mrp(s, ld, st),
    'Discount'           : lambda s, ld, st, d=None: parse_discount(s, ld, st),
    'Rating'             : lambda s, ld, st, d=None: parse_rating(s, ld, st),
    'Rating Count'       : lambda s, ld, st, d=None: parse_rating_count(s, ld, st),
    'Review Count'       : lambda s, ld, st, d=None: parse_review_count(s, ld, st),
    'Pack Size'          : lambda s, ld, st, d=None: parse_pack_size(s, ld, st),
    'Seller'             : lambda s, ld, st, d=None: parse_seller(s, ld, st, d),
    'Seller Rating'      : lambda s, ld, st, d=None: parse_seller_rating(s, ld, st),
    'Availability'       : lambda s, ld, st, d=None: parse_availability(s, ld, st),
    'Highlights'         : lambda s, ld, st, d=None: parse_highlights(s, ld, st),
    'Image URL'          : lambda s, ld, st, d=None: parse_image_url(s, ld, st),
}


def scrape_one(driver, fsn, selected_fields):
    url = f"https://www.flipkart.com/product/p/itme?pid={fsn}"
    row = {
        "FSN": fsn,
        "Scraped At": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "Status": "",
    }
    soup = load_page(driver, url)
    if soup is None:
        row["Status"] = "FAILED: No Response"
        return row

    pg = soup.get_text(" ", strip=True).lower()
    if any(x in pg for x in ["captcha", "unusual traffic", "access denied"]):
        row["Status"] = "BLOCKED"
        return row
    if any(x in pg for x in ["page not found", "doesn't exist", "oops! looks like"]):
        row["Status"] = "NOT FOUND"
        return row

    ld = extract_jsonld(soup)
    state = extract_initial_state(soup)

    for field in selected_fields:
        try:
            row[field] = PARSERS[field](soup, ld, state, driver)
        except Exception as e:
            row[field] = f"ERR: {e}"

    row["Status"] = "SUCCESS"
    return row


# ═══════════════════════════════════════════════════════════════
#  RUN LOOP  — takes an already-open, already-logged-in driver
# ═══════════════════════════════════════════════════════════════
def scrape(driver, fsns, selected_fields, progress_cb=None, stop_flag=None):
    """
    driver         : Selenium driver already opened via open_driver() with the
                      user logged in and pincode set.
    fsns           : list[str] raw FSNs or full Flipkart product URLs
    selected_fields: list[str] subset of DEFAULT_FIELDS keys to scrape
    progress_cb    : optional callable(idx, total, row_dict)
    stop_flag      : optional common.StopFlag()
    Returns list[dict].
    """
    ids = [extract_id(f) for f in fsns]
    results = []
    for idx, fsn in enumerate(ids, 1):
        if stop_flag and stop_flag.stop:
            break
        row = scrape_one(driver, fsn, selected_fields)
        results.append(row)
        if progress_cb:
            progress_cb(idx, len(ids), row)
    return results
