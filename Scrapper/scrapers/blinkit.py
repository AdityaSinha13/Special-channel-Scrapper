"""
Blinkit product scraper — refactored from Aditya's original standalone script
into a reusable module the Streamlit app can drive with live progress.

Extraction is unchanged: 4-layer fallback chain
  1. window.__PRELOADED_STATE__ (or Next.js __NEXT_DATA__ / redux state)
  2. JSON-LD <script type="application/ld+json">
  3. OG / meta tags
  4. Live DOM XPath scraping (last resort)

Public API:
    ALL_COLS         -> full ordered list of output columns
    DEFAULT_FIELDS    -> dict of column -> bool, sane defaults for the UI checkboxes
    extract_id(raw)   -> normalises a pasted URL or bare PID into a PID
    scrape(pids, cfg, progress_cb=None, stop_flag=None) -> list[dict]
        cfg = {"pincode": "122017", "headless": False, "delay_sec": 3.5, "max_retries": 2}
        progress_cb(idx, total, row_dict) is called after every product (for live UI updates)
"""
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException, WebDriverException
from bs4 import BeautifulSoup
from time import sleep
import json, re, time, random

from .common import make_chrome_driver

ALL_COLS = [
    "PID", "Product Name", "Brand", "Category",
    "Selling Price (Rs)", "MRP (Rs)", "Discount %", "Quantity / Size",
    "Availability", "Delivery Time", "Rating", "Rating Count", "Seller",
    "Product Image URL", "Scraping Status", "Error Message", "Source URL",
]

DEFAULT_FIELDS = {
    "PID": True, "Product Name": True, "Brand": False, "Category": False,
    "Selling Price (Rs)": True, "MRP (Rs)": True, "Discount %": False,
    "Quantity / Size": False, "Availability": True, "Delivery Time": False,
    "Rating": False, "Rating Count": False, "Seller": False,
    "Product Image URL": False, "Scraping Status": True, "Error Message": False,
    "Source URL": False,
}


def extract_id(raw: str) -> str:
    v = str(raw).strip()
    m = re.search(r"/prid/(\d+)", v)
    return m.group(1) if m else v


def set_location(driver, pincode):
    wait = WebDriverWait(driver, 30)
    driver.get("https://blinkit.com")
    sleep(5)

    location_btn_xpaths = [
        '//button[contains(@class,"HeaderSelectLocation")]',
        '//div[contains(@class,"HeaderSelectLocation")]',
        '//button[contains(@class,"location")]',
        '//*[@data-testid="location-selector"]',
        '//*[contains(text(),"Delivery in") or contains(text(),"Set location") or contains(text(),"Deliver to")]',
        '//button[contains(translate(.,"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz"),"location")]',
    ]
    for xp in location_btn_xpaths:
        try:
            btn = WebDriverWait(driver, 4).until(EC.element_to_be_clickable((By.XPATH, xp)))
            btn.click()
            sleep(2)
            break
        except Exception:
            pass

    input_xpaths = [
        '//input[@id="location-input"]',
        '//input[@placeholder and (contains(translate(@placeholder,"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz"),"pincode") or contains(translate(@placeholder,"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz"),"search") or contains(translate(@placeholder,"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz"),"location") or contains(translate(@placeholder,"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz"),"area"))]',
        '//input[@type="text"]',
        '//input[@type="search"]',
    ]
    inp = None
    for xp in input_xpaths:
        try:
            inp = WebDriverWait(driver, 8).until(EC.presence_of_element_located((By.XPATH, xp)))
            inp.clear()
            sleep(0.5)
            for ch in str(pincode):
                inp.send_keys(ch)
                sleep(random.uniform(0.05, 0.15))
            sleep(3)
            break
        except Exception:
            pass

    if inp is None:
        return False

    dropdown_xpaths = [
        '//div[contains(@class,"LocationSearchList__LocationDetailContainer")]',
        '//*[contains(@class,"location-list-item")][1]',
        '//*[contains(@class,"suggestion-item")][1]',
        '//*[@data-testid="location-item"][1]',
        '//ul[contains(@class,"search")]//li[1]',
        '(//li | //div[contains(@class,"item")])[last()-4]',
    ]
    clicked = False
    for xp in dropdown_xpaths:
        try:
            el = WebDriverWait(driver, 6).until(EC.element_to_be_clickable((By.XPATH, xp)))
            el.click()
            clicked = True
            sleep(3)
            break
        except Exception:
            pass

    if not clicked:
        try:
            inp.send_keys(Keys.RETURN)
            sleep(2)
        except Exception:
            pass
    sleep(2)
    return True


def _clean_price(text):
    if not text:
        return ""
    # Handle both ₹ unicode and plain numbers
    cleaned = re.sub(r"[^\d.]", "", str(text).replace(",", ""))
    # Remove multiple dots
    parts = cleaned.split(".")
    if len(parts) > 2:
        cleaned = parts[0] + "." + "".join(parts[1:])
    return cleaned if cleaned else ""


def _deep_find(obj, key, depth=0, max_depth=8):
    """FIX #9: Added depth limit to prevent infinite recursion on huge JSON."""
    if depth > max_depth:
        return None
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            r = _deep_find(v, key, depth + 1, max_depth)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for i in obj[:20]:  # FIX: Don't scan more than 20 list items
            r = _deep_find(i, key, depth + 1, max_depth)
            if r is not None:
                return r
    return None


def _cart_item_walk(data):
    """FIX #3: Fixed stepper_data_v2 logic + added more action key variants."""
    ci = {}
    for k in ("rfc_actions_v2", "atc_actions_v2", "stepper_data_v2", "actions"):
        blk = data.get(k) or {}

        # FIX: stepper_data_v2 has different structure
        if k == "stepper_data_v2":
            inc = blk.get("increment_actions") or {}
            cands = inc.get("default") or inc.get("actions") or []
        else:
            cands = blk.get("default") or blk.get("actions") or []

        if not isinstance(cands, list):
            cands = [cands] if cands else []

        for act in cands:
            if not act or not isinstance(act, dict):
                continue
            for an in ("add_to_cart", "remove_from_cart", "atc", "rfc"):
                found = (act.get(an) or {}).get("cart_item") or {}
                if not found:
                    # Sometimes cart_item is nested differently
                    found = act.get("cart_item") or {}
                if found:
                    ci.update({x: y for x, y in found.items() if y and x not in ci})

        if ci.get("brand") and ci.get("mrp"):
            break
    return ci


# ═══════════════════════════════════════════════════════════════
#  METHOD 1 — PRELOADED_STATE  (FIX #1: Fixed regex)
# ═══════════════════════════════════════════════════════════════
def parse_preloaded_state(page_src):
    """FIX #1: Multiple regex patterns to handle different state variable names."""
    state = None

    # Try different variable names Blinkit uses
    patterns = [
        # Current Blinkit pattern
        r"window\.__PRELOADED_STATE__\s*=\s*(\{.+?\})\s*(?:;|</script>)",
        # Older grofers pattern
        r"window\.grofers\.PRELOADED_STATE\s*=\s*(\{.+?\})\s*(?:;|window\.)",
        # Generic __NEXT_DATA__ (Next.js)
        r'<script id="__NEXT_DATA__"[^>]*>(\{.+?\})\s*</script>',
        # __REDUX_STATE__
        r"window\.__REDUX_STATE__\s*=\s*(\{.+?\})\s*;",
    ]

    for pattern in patterns:
        # FIX: Use non-greedy with DOTALL, search from end of match
        matches = list(re.finditer(pattern, page_src, re.DOTALL))
        for m in matches:
            raw = m.group(1)
            # Validate it's proper JSON by checking balance
            try:
                state = json.loads(raw)
                if state and isinstance(state, dict):
                    return state
            except json.JSONDecodeError:
                # Try to find balanced JSON
                try:
                    state = _extract_balanced_json(page_src, m.start(1))
                    if state:
                        return state
                except Exception:
                    pass
    return None


def _extract_balanced_json(src, start_pos):
    """Extract balanced JSON object from string starting at position."""
    depth = 0
    in_string = False
    escape_next = False
    i = start_pos

    while i < len(src) and i < start_pos + 2_000_000:  # 2MB limit
        c = src[i]
        if escape_next:
            escape_next = False
        elif c == "\\" and in_string:
            escape_next = True
        elif c == '"':
            in_string = not in_string
        elif not in_string:
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(src[start_pos:i + 1])
                    except Exception:
                        return None
        i += 1
    return None


def _parse_state_data(state, pid):
    out = {}
    snippets = []

    # Try multiple paths to find snippets
    paths = [
        ["ui", "pdp", "bffPdp", "bffData", "snippets"],
        ["ui", "pdp", "bffData", "snippets"],
        ["pdp", "bffData", "snippets"],
        ["bffData", "snippets"],
    ]
    for path in paths:
        node = state
        try:
            for key in path:
                node = node[key]
            if isinstance(node, list) and node:
                snippets = node
                break
        except (KeyError, TypeError):
            pass

    if not snippets:
        # Last resort: deep find
        snippets = _deep_find(state, "snippets") or []

    if not snippets:
        return out

    # Name + rating + category
    for s in snippets:
        if not isinstance(s, dict):
            continue
        wt = s.get("widget_type", "")
        if any(x in wt for x in ("text_right_icons", "header", "rating", "pdp_top", "product_header")):
            d = s.get("data") or {}
            t = d.get("title") or {}
            if not out.get("Product Name"):
                out["Product Name"] = (
                    t.get("text") if isinstance(t, dict)
                    else str(t) if t else ""
                )
            for rk in ("rating_data", "rating", "ratings"):
                rd = d.get(rk)
                if isinstance(rd, dict):
                    for fk in ("rating", "text", "average_rating"):
                        if rd.get(fk):
                            out["Rating"] = str(rd[fk])
                            break
                    for fk in ("rating_count", "review_count", "count"):
                        if rd.get(fk):
                            out["Rating Count"] = str(rd[fk])
                            break
                    if out.get("Rating"):
                        break
            ca = (s.get("tracking") or {}).get("common_attributes") or {}
            cats = [ca.get(x) for x in ("l0_category", "l1_category", "l2_category") if ca.get(x)]
            if cats:
                out["Category"] = " > ".join(cats)
            if out.get("Product Name"):
                break

    # ATC strip
    for s in snippets:
        if not isinstance(s, dict):
            continue
        wt = s.get("widget_type", "")
        if any(x in wt for x in ("atc", "product_atc", "add_to_cart", "buy_now")):
            d = s.get("data") or {}
            v = d.get("variant") or d.get("quantity") or {}
            out["Quantity / Size"] = out.get("Quantity / Size") or (
                v.get("text") if isinstance(v, dict) else str(v) if v else ""
            )
            np_ = d.get("normal_price") or d.get("price") or {}
            pt = np_.get("text") or np_.get("value") or ""
            if pt:
                out["Selling Price (Rs)"] = _clean_price(pt)
            out["Availability"] = "Out of Stock" if d.get("is_sold_out") else "In Stock"
            ci = _cart_item_walk(d)
            if ci:
                out["Brand"] = out.get("Brand") or ci.get("brand") or ci.get("brand_name") or ""
                out["Seller"] = out.get("Seller") or ci.get("seller_name") or ci.get("merchant_name") or ""
                out["Product Image URL"] = out.get("Product Image URL") or ci.get("image_url") or ""
                out["Quantity / Size"] = out.get("Quantity / Size") or ci.get("unit") or ci.get("quantity") or ""
                if not out.get("Product Name"):
                    out["Product Name"] = ci.get("product_name") or ci.get("display_name") or ci.get("name") or ""
                for mk in ("mrp", "market_price", "max_retail_price"):
                    v2 = ci.get(mk)
                    if v2 not in (None, "", 0, 0.0):
                        out["MRP (Rs)"] = _clean_price(str(v2))
                        break
                if not out.get("Selling Price (Rs)"):
                    for pk in ("price", "selling_price", "discounted_price"):
                        v2 = ci.get(pk)
                        if v2 not in (None, "", 0, 0.0):
                            out["Selling Price (Rs)"] = _clean_price(str(v2))
                            break
            break

    # Fallback: scan all snippets for cart_item
    if not out.get("Brand") or not out.get("MRP (Rs)"):
        for s in snippets:
            if not isinstance(s, dict):
                continue
            ci = _cart_item_walk(s.get("data") or {})
            if ci:
                out["Brand"] = out.get("Brand") or ci.get("brand") or ""
                out["Seller"] = out.get("Seller") or ci.get("seller_name") or ""
                out["Product Image URL"] = out.get("Product Image URL") or ci.get("image_url") or ""
                if not out.get("MRP (Rs)"):
                    for mk in ("mrp", "market_price"):
                        v2 = ci.get(mk)
                        if v2 not in (None, "", 0, 0.0):
                            out["MRP (Rs)"] = _clean_price(str(v2))
                            break
                if not out.get("Selling Price (Rs)"):
                    for pk in ("price", "selling_price"):
                        v2 = ci.get(pk)
                        if v2 not in (None, "", 0, 0.0):
                            out["Selling Price (Rs)"] = _clean_price(str(v2))
                            break
                if out.get("Brand") and out.get("MRP (Rs)"):
                    break

    # Image fallback from carousel
    if not out.get("Product Image URL"):
        for s in snippets:
            if not isinstance(s, dict):
                continue
            wt = (s.get("widget_type") or "").lower()
            if "carousal" in wt or "carousel" in wt or "image" in wt:
                for item in ((s.get("data") or {}).get("itemList") or []):
                    img = (
                        (((item.get("data") or {}).get("media_content") or {}).get("image") or {}).get("url", "")
                        or (item.get("data") or {}).get("image_url", "")
                    )
                    if img:
                        out["Product Image URL"] = img
                        break
                if out.get("Product Image URL"):
                    break

    # Delivery ETA
    for key in ("eta_in_string", "delivery_message", "eta", "promise", "sla"):
        val = _deep_find(state, key)
        if isinstance(val, str) and val:
            out["Delivery Time"] = val
            break

    return out


# ═══════════════════════════════════════════════════════════════
#  METHOD 2 — JSON-LD
# ═══════════════════════════════════════════════════════════════
def parse_json_ld(soup):
    out = {}
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            raw = tag.string or ""
            if not raw.strip():
                continue
            data = json.loads(raw)
            if isinstance(data, list):
                data = next((d for d in data if isinstance(d, dict) and "Product" in str(d.get("@type", ""))), {})
            if not isinstance(data, dict):
                continue
            t = data.get("@type", "")
            if "Product" in str(t) or "ItemPage" in str(t):
                out["Product Name"] = out.get("Product Name") or data.get("name", "")
                brand = data.get("brand") or {}
                out["Brand"] = out.get("Brand") or (
                    brand.get("name", "") if isinstance(brand, dict) else str(brand)
                )
                img = data.get("image")
                if img:
                    out["Product Image URL"] = out.get("Product Image URL") or (
                        img[0] if isinstance(img, list) else str(img)
                    )
                agg = data.get("aggregateRating") or {}
                if agg:
                    out["Rating"] = out.get("Rating") or str(agg.get("ratingValue", ""))
                    out["Rating Count"] = out.get("Rating Count") or str(
                        agg.get("reviewCount") or agg.get("ratingCount") or ""
                    )
                offers = data.get("offers") or {}
                if isinstance(offers, list):
                    offers = offers[0] if offers else {}
                out["Selling Price (Rs)"] = out.get("Selling Price (Rs)") or _clean_price(
                    str(offers.get("price", ""))
                )
                avail = str(offers.get("availability", ""))
                if avail and not out.get("Availability"):
                    out["Availability"] = "In Stock" if "InStock" in avail else (
                        "Out of Stock" if "OutOfStock" in avail else ""
                    )
                out["Category"] = out.get("Category") or data.get("category", "")
                desc = data.get("description", "")
                if desc and not out.get("Quantity / Size"):
                    # Try to extract weight/quantity from description
                    wm = re.search(r"(\d+(?:\.\d+)?\s*(?:g|kg|ml|l|L|gm|ltr|pack|pcs|pieces|units))", desc, re.I)
                    if wm:
                        out["Quantity / Size"] = wm.group(1)
        except Exception:
            pass
    return out


# ═══════════════════════════════════════════════════════════════
#  METHOD 3 — META TAGS  (FIX #8: Better price regex)
# ═══════════════════════════════════════════════════════════════
def parse_meta_tags(soup):
    out = {}

    def m(prop, name=None):
        tag = soup.find("meta", property=prop) or (
            soup.find("meta", attrs={"name": name}) if name else None
        )
        return (tag.get("content") or "").strip() if tag else ""

    title = m("og:title") or m(None, "title") or (soup.title.text if soup.title else "")
    if title:
        out["Product Name"] = re.split(r"\s*[|\-–—]\s*(?:Blinkit|blinkit)", title, flags=re.I)[0].strip()

    out["Product Image URL"] = m("og:image")
    out["Availability"] = "In Stock" if "instock" in m("product:availability").lower() else ""

    desc = m("og:description") or m(None, "description")
    if desc:
        # FIX #8: Handle both ₹ unicode AND "Rs." text
        pm = re.search(r"(?:₹|Rs\.?\s*)[\s]*([\d,]+(?:\.\d+)?)", desc)
        if pm:
            out["Selling Price (Rs)"] = pm.group(1).replace(",", "")

    return out


# ═══════════════════════════════════════════════════════════════
#  METHOD 4 — LIVE DOM
# ═══════════════════════════════════════════════════════════════
def parse_dom_selenium(driver):
    out = {}

    def get(xpaths):
        for xp in xpaths:
            try:
                el = driver.find_element(By.XPATH, xp)
                t = el.text.strip()
                if t:
                    return t
            except Exception:
                pass
        return ""

    def get_attr(xpaths, attr):
        for xp in xpaths:
            try:
                el = driver.find_element(By.XPATH, xp)
                v = el.get_attribute(attr)
                if v:
                    return v.strip()
            except Exception:
                pass
        return ""

    out["Product Name"] = get(["//h1"])

    out["Selling Price (Rs)"] = _clean_price(get([
        '//*[@data-testid="product-price"]',
        '//*[contains(@class,"ProductVariants__Price") and not(contains(@class,"strike"))]',
        '//*[contains(@class,"Price__") and not(contains(@class,"strike")) and not(contains(@class,"Mrp")) and not(contains(@class,"mrp"))]',
        # Any span with ₹ that is not struck through
        '//span[contains(text(),"₹") and not(ancestor::*[contains(@style,"line-through") or contains(@class,"strike")])]',
    ]))

    out["MRP (Rs)"] = _clean_price(get([
        '//*[@data-testid="product-mrp"]',
        '//*[contains(@class,"Mrp") or contains(@class,"mrp")]',
        '//*[contains(@style,"line-through")]',
        '//span[contains(@class,"strike") or contains(@class,"Strike")]',
    ]))

    out["Quantity / Size"] = get([
        '//*[@data-testid="product-weight"]',
        '//*[contains(@class,"Weight") or contains(@class,"weight")]',
        '//*[contains(@class,"Variant") or contains(@class,"variant")][1]',
        '//button[contains(@class,"variant") or contains(@class,"Variant")][1]',
    ])

    out["Brand"] = get([
        '//*[@data-testid="product-brand"]',
        '//*[contains(@class,"Brand") or contains(@class,"brand")]',
        '//a[contains(@href,"/brand/")]',
        '//span[contains(@class,"ProductDetail__BrandName")]',
    ])

    sold_out = get([
        '//*[contains(@class,"SoldOut") or contains(@class,"sold-out") or contains(@class,"OutOfStock")]',
        '//button[contains(text(),"Sold") or contains(text(),"Out of Stock")]',
        '//*[contains(text(),"Currently Unavailable")]',
    ])
    out["Availability"] = "Out of Stock" if sold_out else "In Stock"

    out["Delivery Time"] = get([
        '//*[@data-testid="delivery-time"]',
        '//*[contains(@class,"DeliveryTime") or contains(@class,"delivery-time")]',
        '//*[contains(@class,"Eta") or contains(@class,"eta")]',
        '//span[contains(text(),"min")]',
        '//div[contains(text(),"min delivery")]',
    ])

    out["Rating"] = get([
        '//*[@data-testid="product-rating"]',
        '//*[contains(@class,"Rating") and not(contains(@class,"Count"))]//span[1]',
        '//span[contains(@class,"ProductRating")]',
    ])

    out["Rating Count"] = get([
        '//*[@data-testid="rating-count"]',
        '//*[contains(@class,"RatingCount") or contains(@class,"rating-count")]',
        '//span[contains(text(),"rating") or contains(text(),"review")]',
    ])

    # Category from breadcrumb
    try:
        crumbs = driver.find_elements(By.XPATH,
            '//nav[contains(@class,"breadcrumb") or contains(@class,"Breadcrumb")]//a '
            '| //ol//li//a | //div[contains(@class,"Breadcrumb")]//a'
        )
        if crumbs:
            out["Category"] = " > ".join(c.text.strip() for c in crumbs if c.text.strip())
    except Exception:
        pass

    out["Seller"] = get([
        '//*[contains(@class,"Seller") or contains(@class,"seller") or contains(@class,"merchant")]',
        '//span[contains(text(),"Sold by")]/following-sibling::span[1]',
        '//*[contains(@class,"SoldBy")]',
    ])

    out["Product Image URL"] = get_attr([
        '//*[@data-testid="product-image"]//img',
        '//*[contains(@class,"ProductImage") or contains(@class,"product-image")]//img',
        '//div[contains(@class,"carousel") or contains(@class,"Carousel")]//img[1]',
        '//img[contains(@alt,"product") or contains(@class,"Product")]',
        '//main//img[1]',
    ], "src")

    return out


# ═══════════════════════════════════════════════════════════════
#  MASTER EXTRACTOR
# ═══════════════════════════════════════════════════════════════
def extract_product(driver, pid):
    """FIX #4: All fields initialized + complete result dict always returned."""
    result = {col: "" for col in ALL_COLS}
    result["PID"] = pid
    result["Scraping Status"] = "Failed"
    result["Source URL"] = driver.current_url

    try:
        page_src = driver.page_source
        soup = BeautifulSoup(page_src, "html.parser")

        # ── Method 1: PRELOADED_STATE ──
        state = parse_preloaded_state(page_src)
        if state:
            data = _parse_state_data(state, pid)
            for k, v in data.items():
                if v and not result.get(k):
                    result[k] = v

        # ── Method 2: JSON-LD ──
        if not result.get("Product Name"):
            data = parse_json_ld(soup)
            for k, v in data.items():
                if v and not result.get(k):
                    result[k] = v

        # ── Method 3: Meta tags ──
        if not result.get("Product Name"):
            data = parse_meta_tags(soup)
            for k, v in data.items():
                if v and not result.get(k):
                    result[k] = v

        # ── Method 4: Live DOM (if any key fields missing) ──
        missing = [k for k in ("Product Name", "Selling Price (Rs)", "MRP (Rs)", "Quantity / Size", "Brand")
                   if not result.get(k)]
        if missing:
            data = parse_dom_selenium(driver)
            for k, v in data.items():
                if v and not result.get(k):
                    result[k] = v

    except Exception as e:
        result["Error Message"] = f"Parse error: {str(e)[:200]}"

    # ── Discount % calc ──
    if not result.get("Discount %") and result.get("MRP (Rs)") and result.get("Selling Price (Rs)"):
        try:
            mrp = float(result["MRP (Rs)"])
            sp  = float(result["Selling Price (Rs)"])
            if mrp > 0 and sp > 0 and mrp >= sp:
                result["Discount %"] = str(round((mrp - sp) / mrp * 100, 1)) + "%"
        except (ValueError, ZeroDivisionError):
            pass

    # ── Status ──
    if result.get("Product Name") or result.get("Selling Price (Rs)"):
        result["Scraping Status"] = "Success"
        result["Error Message"] = ""
    else:
        result["Scraping Status"] = "Failed"
        if not result["Error Message"]:
            result["Error Message"] = "No data found — PID invalid, blocked, or page structure changed"

    return result

# ═══════════════════════════════════════════════════════════════
#  DRIVER-DRIVEN RUN LOOP  (replaces the original script's main())
# ═══════════════════════════════════════════════════════════════
def scrape(pids, cfg, progress_cb=None, stop_flag=None):
    """
    pids       : list[str] raw PIDs or full Blinkit URLs
    cfg        : {"pincode": str, "headless": bool, "delay_sec": float, "max_retries": int}
    progress_cb: optional callable(idx, total, row_dict)
    stop_flag  : optional common.StopFlag() — set .stop = True to abort early
    Returns list[dict] with columns from ALL_COLS.
    """
    pincode = cfg.get("pincode", "122017")
    headless = cfg.get("headless", False)
    delay_sec = cfg.get("delay_sec", 3.5)
    max_retries = cfg.get("max_retries", 2)

    ids = [extract_id(p) for p in pids]
    results = []
    driver = None
    try:
        driver = make_chrome_driver(headless=headless)
        set_location(driver, pincode)

        for idx, pid in enumerate(ids, 1):
            if stop_flag and stop_flag.stop:
                break

            data = None
            url = f"https://blinkit.com/prn/x/prid/{pid}"

            for attempt in range(1, max_retries + 1):
                try:
                    driver.get(url)
                    try:
                        WebDriverWait(driver, 15).until(
                            EC.presence_of_element_located((By.XPATH, "//h1 | //main | //body"))
                        )
                    except TimeoutException:
                        pass
                    sleep(delay_sec + random.uniform(0.5, 2.0))

                    current = driver.current_url
                    if "blinkit.com" not in current:
                        raise Exception(f"Redirect to: {current}")

                    data = extract_product(driver, pid)
                    if data["Scraping Status"] == "Failed" and attempt < max_retries:
                        sleep(2)
                        continue
                    break
                except WebDriverException as e:
                    if attempt < max_retries:
                        sleep(3)
                    else:
                        data = {col: "" for col in ALL_COLS}
                        data["PID"] = pid
                        data["Scraping Status"] = "Failed"
                        data["Error Message"] = f"WebDriver error: {str(e)[:150]}"
                        data["Source URL"] = url

            if data is None:
                data = {col: "" for col in ALL_COLS}
                data["PID"] = pid
                data["Scraping Status"] = "Failed"
                data["Error Message"] = "Unknown error"
                data["Source URL"] = url

            results.append(data)
            if progress_cb:
                progress_cb(idx, len(ids), data)

    finally:
        if driver:
            driver.quit()

    return results
