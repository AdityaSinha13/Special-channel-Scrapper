"""
Amazon.in ASIN scraper — refactored from Aditya's original notebook into a
reusable module. Uses a warmed-up `requests` session for the bulk of parsing
(fast, no browser needed) with an optional Selenium fallback only for the
"live sellers" field (Amazon's offer-listing page is JS-heavy).

Public API mirrors the other platform modules:
    ALL_COLS, DEFAULT_FIELDS, extract_id(raw), scrape(asins, cfg, progress_cb, stop_flag)
    cfg = {
        "fields": {...} ,       # which SCRAPE_* toggles are on (see DEFAULT_FIELDS)
        "max_retries": 3, "save_every": 5,
        "download_images": False, "images_dir": "amazon_images",
    }
"""
import os
import re
import json
import time
import random
import requests
from bs4 import BeautifulSoup
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException

from .common import make_chrome_driver

ALL_COLS = [
    "12NC", "ASIN", "Title", "Rating", "Reviews", "Buybox Seller", "Buybox Price",
    "MRP", "SP", "Discount %", "Availability", "Brand", "Category",
    "Bullet Points", "Description", "Fulfilled By", "Stock Count Text",
    "Coupon", "Bank Offers", "Bestseller Rank", "Variation Count",
    "Total Live Sellers", "Image URLs", "Images Downloaded",
]

DEFAULT_FIELDS = {
    "Title": True, "Rating": False, "Reviews": False, "Buybox Seller/Price": True,
    "MRP/SP/Discount/Availability": True, "Brand": False, "Category": False,
    "Bullet Points": False, "Description": False, "Fulfilled By": False,
    "Stock Count": False, "Coupon": False, "Bank Offers": False,
    "Bestseller Rank": False, "Variation Count": False,
    "All Live Sellers (slow, needs browser)": False, "Images": False,
}


def extract_id(raw: str) -> str:
    """Normalise a pasted Amazon URL or bare ASIN into a 10-char ASIN."""
    v = str(raw).strip()
    m = re.search(r"/dp/([A-Z0-9]{10})", v, re.I)
    if m:
        return m.group(1).upper()
    m = re.search(r"/gp/product/([A-Z0-9]{10})", v, re.I)
    if m:
        return m.group(1).upper()
    return v.upper()



# ===========================================================
#  HELPERS
# ===========================================================

def get_headers(asin=None):
    referer = ("https://www.amazon.in/dp/" + asin) if asin else "https://www.amazon.in/"
    return {
        "User-Agent": random.choice([
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
        ]),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-IN,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": referer,
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "sec-fetch-dest": "document",
        "sec-fetch-mode": "navigate",
        "sec-fetch-site": "same-origin",
        "sec-fetch-user": "?1",
        "Cache-Control": "max-age=0",
    }

def is_captcha_soup(soup):
    if soup.find("form", action="/errors/validateCaptcha"):
        return True
    title_tag = soup.find("title")
    if title_tag:
        t = title_tag.get_text().lower()
        if "robot" in t or "captcha" in t:
            return True
    return False

def is_captcha_driver(driver):
    try:
        src = driver.page_source.lower()
        return "validatecaptcha" in src or "robot check" in src or "captcha" in driver.title.lower()
    except Exception:
        return False

# ===========================================================
#  BUYBOX SELLER + PRICE  (from main product page)
# ===========================================================

def get_buybox_seller_and_price(soup):
    # --- Seller ---
    sold_by = "Not Found"
    sl = soup.find("a", id="sellerProfileTriggerId")
    if sl:
        sold_by = sl.get_text(strip=True)
    if sold_by == "Not Found":
        md = soup.find("div", id="merchant-info")
        if md:
            sold_by = md.get_text(" ", strip=True)
    if sold_by == "Not Found":
        bb = soup.find("div", id="tabular-buybox")
        if bb:
            for row in bb.find_all("div", class_="tabular-buybox-container"):
                label = row.find("div", class_="tabular-buybox-text")
                if label and "sold by" in label.get_text(strip=True).lower():
                    vals = row.find_all("div", class_="tabular-buybox-text")
                    if len(vals) > 1:
                        sold_by = vals[-1].get_text(strip=True)
                        break

    # --- Price ---
    buybox_price = "Not Found"

    # Method 1: corePriceDisplay_desktop_feature_div (most reliable for current page)
    core_div = soup.find("div", id="corePriceDisplay_desktop_feature_div")
    if core_div:
        whole = core_div.find("span", class_="a-price-whole")
        frac  = core_div.find("span", class_="a-price-fraction")
        if whole:
            w = whole.get_text(strip=True).replace(".", "").replace(",", "")
            f = frac.get_text(strip=True) if frac else "00"
            buybox_price = "Rs." + w + "." + f

    # Method 2: priceToPay span
    if buybox_price == "Not Found":
        pay = soup.find("span", class_="priceToPay")
        if pay:
            whole = pay.find("span", class_="a-price-whole")
            frac  = pay.find("span", class_="a-price-fraction")
            if whole:
                w = whole.get_text(strip=True).replace(".", "").replace(",", "")
                f = frac.get_text(strip=True) if frac else "00"
                buybox_price = "Rs." + w + "." + f

    # Method 3: Legacy price IDs
    if buybox_price == "Not Found":
        for tag, attrs in [
            ("span", {"id": "priceblock_ourprice"}),
            ("span", {"id": "priceblock_dealprice"}),
            ("span", {"id": "price_inside_buybox"}),
        ]:
            pt = soup.find(tag, attrs)
            if pt:
                buybox_price = pt.get_text(strip=True)
                break

    return sold_by, buybox_price

# ===========================================================
#  MRP (List Price) + SP (Selling Price) + Discount % + Availability
# ===========================================================

def get_mrp_sp_discount(soup):
    """
    Returns (mrp, sp, discount_pct, availability) for the single ASIN's page.
    MRP  = strikethrough "M.R.P." price
    SP   = current selling price (same as buybox price, but kept separate
           here so this function is self-contained / reusable)
    """
    mrp = "Not Found"
    sp = "Not Found"
    discount_pct = "Not Found"
    availability = "Not Found"

    # --- SP (Selling Price) ---
    # Method 1: corePriceDisplay_desktop_feature_div (current Amazon layout)
    core_div = soup.find("div", id="corePriceDisplay_desktop_feature_div")
    if core_div:
        whole = core_div.find("span", class_="a-price-whole")
        frac  = core_div.find("span", class_="a-price-fraction")
        if whole:
            w = whole.get_text(strip=True).replace(".", "").replace(",", "")
            f = frac.get_text(strip=True) if frac else "00"
            sp = "Rs." + w + "." + f

    # Method 2: priceToPay span (works even outside corePriceDisplay block)
    if sp == "Not Found":
        pay = soup.find("span", class_="priceToPay")
        if pay:
            whole = pay.find("span", class_="a-price-whole")
            frac  = pay.find("span", class_="a-price-fraction")
            if whole:
                w = whole.get_text(strip=True).replace(".", "").replace(",", "")
                f = frac.get_text(strip=True) if frac else "00"
                sp = "Rs." + w + "." + f

    # Method 3: generic a-price not flagged as a strikethrough/basis price
    if sp == "Not Found":
        for price_span in soup.find_all("span", class_="a-price"):
            parent_classes = " ".join(price_span.get("class", []))
            if "a-text-price" in parent_classes:
                continue  # this is the MRP style, skip
            offscreen = price_span.find("span", class_="a-offscreen")
            if offscreen and offscreen.get_text(strip=True):
                sp = offscreen.get_text(strip=True)
                break

    # Method 4: Legacy price IDs
    if sp == "Not Found":
        for tag, attrs in [
            ("span", {"id": "priceblock_ourprice"}),
            ("span", {"id": "priceblock_dealprice"}),
            ("span", {"id": "price_inside_buybox"}),
        ]:
            pt = soup.find(tag, attrs)
            if pt:
                sp = pt.get_text(strip=True)
                break

    # --- MRP (List Price / strikethrough) ---
    # Method 1: basisPrice block (most common current layout)
    basis_div = soup.find("span", id="basisPrice") or soup.find("span", class_="basisPrice")
    if basis_div:
        strike = basis_div.find("span", class_="a-text-price")
        if strike:
            off = strike.find("span", class_="a-offscreen")
            if off:
                mrp = off.get_text(strip=True)

    # Method 2: any a-text-price span on the page (strikethrough MRP)
    if mrp == "Not Found":
        strike = soup.find("span", class_="a-text-price")
        if strike:
            off = strike.find("span", class_="a-offscreen")
            if off and off.get_text(strip=True):
                mrp = off.get_text(strip=True)
            else:
                txt = strike.get_text(strip=True)
                if txt:
                    mrp = txt

    # Method 3: "M.R.P.:" label followed by price text
    if mrp == "Not Found":
        mrp_label = soup.find(string=re.compile(r"M\.R\.P\.?", re.I))
        if mrp_label:
            parent = mrp_label.find_parent()
            if parent:
                nxt = parent.find_next("span", class_="a-offscreen")
                if nxt:
                    mrp = nxt.get_text(strip=True)

    # Method 4: legacy strikethrough id
    if mrp == "Not Found":
        pt = soup.find("span", id="priceblock_strikeprice") or soup.find("span", id="listPrice")
        if pt:
            mrp = pt.get_text(strip=True)

    # If MRP isn't found but SP is, treat SP as MRP too (no discount running)
    if mrp == "Not Found" and sp != "Not Found":
        mrp = sp

    # --- Discount % ---
    # Method 1: explicit "X% off" / savingsPercentage badge on page
    savings_tag = soup.find("span", class_=re.compile(r"savingsPercentage"))
    if savings_tag:
        discount_pct = savings_tag.get_text(strip=True)

    # Method 2: compute from MRP and SP if both are clean numeric values
    if discount_pct == "Not Found" and mrp != "Not Found" and sp != "Not Found" and mrp != sp:
        try:
            mrp_num = float(re.sub(r"[^\d.]", "", mrp))
            sp_num  = float(re.sub(r"[^\d.]", "", sp))
            if mrp_num > 0 and sp_num > 0 and mrp_num >= sp_num:
                discount_pct = str(round((1 - sp_num / mrp_num) * 100)) + "%"
        except Exception:
            pass

    # --- Availability ---
    avail_div = soup.find("div", id="availability")
    if avail_div:
        span = avail_div.find("span")
        if span:
            availability = span.get_text(strip=True)
        else:
            availability = avail_div.get_text(strip=True)
    if availability == "Not Found":
        msg = soup.find("span", class_="a-color-success") or soup.find("span", class_="a-color-price")
        if msg and msg.get_text(strip=True):
            availability = msg.get_text(strip=True)

    return mrp, sp, discount_pct, availability

# ===========================================================
#  EXTRA PRODUCT DETAILS
#  Brand, Category, Bullets, Description, Fulfilled By,
#  Stock Count, Coupon, Bank Offers, Bestseller Rank, Variations
# ===========================================================

def get_brand(soup):
    brand = "Not Found"
    tag = soup.find("a", id="bylineInfo")
    if tag:
        txt = tag.get_text(strip=True)
        brand = re.sub(r"^(Brand:|Visit the|Store)\s*", "", txt, flags=re.I).strip()
    if brand == "Not Found" or not brand:
        tag = soup.find("tr", string=re.compile("Brand", re.I))
        if tag:
            val = tag.find("td")
            if val:
                brand = val.get_text(strip=True)
    return brand if brand else "Not Found"


def get_category(soup):
    category = "Not Found"
    nav = soup.find("div", id="wayfinding-breadcrumbs_feature_div")
    if nav:
        links = nav.find_all("a")
        if links:
            category = " > ".join(a.get_text(strip=True) for a in links)
    return category


def get_bullets(soup):
    bullets_text = "Not Found"
    div = soup.find("div", id="feature-bullets")
    if div:
        items = div.find_all("span", class_="a-list-item")
        lines = [li.get_text(strip=True) for li in items if li.get_text(strip=True)]
        if lines:
            bullets_text = " | ".join(lines)
    return bullets_text


def get_description(soup):
    description = "Not Found"
    div = soup.find("div", id="productDescription")
    if div:
        txt = div.get_text(" ", strip=True)
        if txt:
            description = txt
    return description


def get_fulfilled_by(soup):
    fulfilled_by = "Not Found"
    ff = soup.find("div", id="fulfillerInfoFeature_feature_div")
    if ff:
        txt = ff.get_text(" ", strip=True)
        if txt:
            fulfilled_by = txt
    if fulfilled_by == "Not Found":
        merchant = soup.find("div", id="merchant-info")
        if merchant:
            txt = merchant.get_text(" ", strip=True)
            if "amazon" in txt.lower():
                fulfilled_by = "Amazon"
            elif txt:
                fulfilled_by = "Seller (" + txt + ")"
    return fulfilled_by


def get_stock_count(soup):
    stock_count = "Not Found"
    avail = soup.find("div", id="availability")
    if avail:
        txt = avail.get_text(" ", strip=True)
        m = re.search(r"only\s+\d+\s+left", txt, re.I)
        if m:
            stock_count = m.group(0)
    return stock_count


def get_coupon(soup):
    coupon = "Not Found"
    tag = soup.find(string=re.compile(r"Apply\s+\d+%?\s*coupon", re.I))
    if tag:
        coupon = tag.strip()
    if coupon == "Not Found":
        cb = soup.find("span", class_=re.compile("couponBadge|promoPriceBlockMessage"))
        if cb:
            txt = cb.get_text(strip=True)
            if txt:
                coupon = txt
    return coupon


def get_bank_offers(soup):
    bank_offers = "Not Found"
    div = soup.find("div", id="vsxoffers_feature_div") or soup.find("div", id="promotions_feature_div")
    if div:
        txt = div.get_text(" | ", strip=True)
        if txt:
            bank_offers = txt[:500]  # cap length to avoid huge cells
    return bank_offers


def get_bestseller_rank(soup):
    rank = "Not Found"
    div = soup.find("div", id="prodDetails") or soup.find("table", id="productDetails_detailBullets_sections1")
    if div:
        txt = div.get_text(" ", strip=True)
        m = re.search(r"#[\d,]+\s+in\s+[A-Za-z &]+", txt)
        if m:
            rank = m.group(0)
    if rank == "Not Found":
        sales_rank_li = soup.find(string=re.compile("Best Sellers Rank", re.I))
        if sales_rank_li:
            parent = sales_rank_li.find_parent()
            if parent:
                txt = parent.get_text(" ", strip=True)
                m = re.search(r"#[\d,]+\s+in\s+[A-Za-z &]+", txt)
                if m:
                    rank = m.group(0)
    return rank


def get_variation_count(soup):
    variation_count = 0
    twister = soup.find("div", id="twister_feature_div")
    if twister:
        swatches = twister.find_all("li", class_=re.compile("swatchAvailable|swatchSelect"))
        if swatches:
            variation_count = len(swatches)
        else:
            buttons = twister.find_all("li", attrs={"data-defaultasin": True})
            variation_count = len(buttons)
    return variation_count

# ===========================================================
#  ALL SELLERS via SELENIUM
# ===========================================================

def get_sellers_selenium(driver, asin):
    """
    Opens offer listing page in real Chrome browser.
    Returns list of dicts: [{"seller": "XYZ", "price": "Rs.999"}]
    """
    sellers = []
    seen    = set()
    offer_url = "https://www.amazon.in/gp/offer-listing/" + asin + "?f_new=true"

    try:
        driver.get(offer_url)
        time.sleep(random.uniform(3, 5))

        if is_captcha_driver(driver):
            print("    [CAPTCHA] Offer page blocked for " + asin)
            return sellers

        page_num = 1

        while True:
            try:
                WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located(
                        (By.CSS_SELECTOR, "#olpOfferList .olpOffer, #aod-offer-list, .olpOffer")
                    )
                )
            except TimeoutException:
                pass

            time.sleep(1.5)
            soup = BeautifulSoup(driver.page_source, "html.parser")

            # Layout 1: Classic olpOffer
            offer_rows = soup.find_all("div", class_=re.compile(r"\bolpOffer\b"))
            # Layout 2: Newer AOD layout
            if not offer_rows:
                offer_rows = soup.find_all("div", id=re.compile(r"^aod-offer(-\d+)?$"))
            if not offer_rows:
                offer_rows = soup.find_all("div", class_=re.compile(r"\baod-offer\b"))

            if not offer_rows:
                print("    [INFO] No seller rows found on page " + str(page_num) + " for " + asin)
                break

            for row in offer_rows:
                # --- Price ---
                price = ""
                whole = row.find("span", class_="a-price-whole")
                frac  = row.find("span", class_="a-price-fraction")
                if whole:
                    w = whole.get_text(strip=True).replace(".", "").replace(",", "")
                    f = frac.get_text(strip=True) if frac else "00"
                    price = "Rs." + w + "." + f
                if not price:
                    for sel in [
                        {"class_": "a-color-price"},
                        {"class_": re.compile(r"olpOfferPrice")},
                        {"class_": re.compile(r"aod-price")},
                    ]:
                        pt = row.find("span", **sel)
                        if pt:
                            price = pt.get_text(strip=True)
                            break

                # --- Seller ---
                seller = ""
                for tag, attr in [
                    ("span", {"class_": "olpSellerName"}),
                    ("a",    {"class_": "olpSellerName"}),
                    ("span", {"class_": re.compile(r"aod-soldBy")}),
                    ("a",    {"class_": re.compile(r"aod-soldBy")}),
                    ("a",    {"href":   re.compile(r"seller=")}),
                ]:
                    s = row.find(tag, **attr)
                    if s:
                        seller = s.get_text(strip=True)
                        if seller:
                            break
                if not seller:
                    if row.find("img", alt=re.compile(r"amazon", re.I)):
                        seller = "Amazon"
                    elif row.find("span", string=re.compile(r"amazon\.in", re.I)):
                        seller = "Amazon"

                key = seller + "|" + price
                if key not in seen and (seller or price):
                    seen.add(key)
                    sellers.append({"seller": seller, "price": price})

            # --- Next Page ---
            next_found = False
            for xpath in [
                "//li[contains(@class,'a-last') and not(contains(@class,'a-disabled'))]/a",
                "//a[contains(text(),'Next page')]",
                "//a[contains(@class,'olpNextLink')]",
            ]:
                try:
                    btn  = driver.find_element(By.XPATH, xpath)
                    href = btn.get_attribute("href")
                    if href:
                        driver.get(href)
                        time.sleep(random.uniform(2, 4))
                        page_num += 1
                        next_found = True
                        break
                except NoSuchElementException:
                    continue

            if not next_found or page_num > 10:
                break

    except Exception as e:
        print("    [SELLER ERROR] " + asin + ": " + str(e))

    return sellers

# ===========================================================
#  IMAGES
# ===========================================================

def get_all_images(soup):
    images = []
    page_text = str(soup)
    for pattern in [
        r"'colorImages':\s*\{[^}]*'initial':\s*(\[.*?\])\s*\}",
        r'"colorImages":\s*\{[^}]*"initial":\s*(\[.*?\])\s*\}',
    ]:
        match = re.search(pattern, page_text, re.DOTALL)
        if match:
            try:
                for item in json.loads(match.group(1)):
                    for key in ["hiRes", "large", "main"]:
                        if item.get(key):
                            url = item[key]
                            if url not in images:
                                images.append(url)
                            break
            except Exception:
                pass
            break
    if not images:
        alt_div = soup.find("div", id="altImages")
        if alt_div:
            for img in alt_div.find_all("img"):
                src  = img.get("src", "")
                full = re.sub(r'\._[A-Z0-9_,]+_\.', '.', src)
                if full and full not in images and "transparent-pixel" not in full:
                    images.append(full)
    if not images:
        for img_id in ["landingImage", "imgBlkFront"]:
            tag = soup.find("img", id=img_id)
            if tag:
                src = tag.get("data-old-hires") or tag.get("src", "")
                if src:
                    images.append(src)
                break
    return images

def download_images(images, nc12, base_dir):
    folder = os.path.join(base_dir, str(nc12))
    os.makedirs(folder, exist_ok=True)
    downloaded = 0
    for idx, url in enumerate(images):
        try:
            ext = "jpg"
            m = re.search(r'\.(jpg|jpeg|png|webp)', url, re.IGNORECASE)
            if m:
                ext = m.group(1).lower()
            filepath = os.path.join(folder, "img_" + str(idx + 1).zfill(2) + "." + ext)
            r = requests.get(url, timeout=10)
            if r.status_code == 200:
                with open(filepath, "wb") as f:
                    f.write(r.content)
                downloaded += 1
        except Exception:
            pass
    return str(downloaded) + "/" + str(len(images)) + " downloaded"

# ===========================================================
#  MAIN SCRAPER
# ===========================================================

def scrape_asin(session, driver, asin, nc12=None, flags=None, max_retries=3, images_dir="amazon_images"):
    """flags: dict with boolean keys — TITLE, RATING, REVIEWS, SOLD_BY, MRP_SP,
    BRAND, CATEGORY, BULLETS, DESCRIPTION, FULFILLED_BY, STOCK_COUNT, COUPON,
    BANK_OFFERS, BESTSELLER_RANK, VARIATIONS, SELLERS, IMAGES."""
    flags = flags or {}
    f = lambda k: flags.get(k, False)
    url = "https://www.amazon.in/dp/" + asin
    result = {
        "12NC"                : nc12 if nc12 else "",
        "ASIN"                : asin,
        "Title"               : "Not Found",
        "Rating"              : "Not Found",
        "Reviews"             : "Not Found",
        "Buybox Seller"       : "Not Found",
        "Buybox Price"        : "Not Found",
        "MRP"                 : "Not Found",
        "SP"                  : "Not Found",
        "Discount %"          : "Not Found",
        "Availability"        : "Not Found",
        "Brand"               : "Not Found",
        "Category"            : "Not Found",
        "Bullet Points"       : "Not Found",
        "Description"         : "Not Found",
        "Fulfilled By"        : "Not Found",
        "Stock Count Text"    : "Not Found",
        "Coupon"              : "Not Found",
        "Bank Offers"         : "Not Found",
        "Bestseller Rank"     : "Not Found",
        "Variation Count"     : 0,
        "Total Live Sellers"  : 0,
        "Image URLs"          : "Not Found",
        "Images Downloaded"   : "N/A",
    }

    for attempt in range(1, max_retries + 1):
        try:
            resp = session.get(url, headers=get_headers(asin), timeout=15)

            if resp.status_code == 404:
                print("  [WARN] " + asin + " -> 404")
                return result

            if resp.status_code != 200:
                print("  [WARN] " + asin + " -> HTTP " + str(resp.status_code))
                time.sleep(random.uniform(5, 10))
                continue

            soup = BeautifulSoup(resp.text, "html.parser")

            if is_captcha_soup(soup):
                print("  [CAPTCHA] " + asin + " attempt " + str(attempt) + " waiting...")
                time.sleep(random.uniform(15, 25))
                continue

            # Title
            if f("TITLE"):
                tag = soup.find("span", id="productTitle")
                result["Title"] = tag.get_text(strip=True) if tag else "Not Found"

            # Rating
            if f("RATING"):
                rating = "Not Found"
                widget = soup.find("div", id="averageCustomerReviews")
                if widget:
                    rt = widget.find("span", class_="a-icon-alt")
                    if rt:
                        rating = rt.get_text(strip=True)
                if rating == "Not Found":
                    rt = soup.find("span", {"data-hook": "rating-out-of-text"})
                    if rt:
                        rating = rt.get_text(strip=True)
                result["Rating"] = rating

            # Reviews
            if f("REVIEWS"):
                rt = soup.find("span", id="acrCustomerReviewText")
                result["Reviews"] = rt.get_text(strip=True) if rt else "Not Found"

            # Buybox Seller + Buybox Price
            if f("SOLD_BY"):
                buybox_seller, buybox_price   = get_buybox_seller_and_price(soup)
                result["Buybox Seller"] = buybox_seller
                result["Buybox Price"]  = buybox_price

            # MRP + SP + Discount % + Availability
            if f("MRP_SP"):
                mrp, sp, discount_pct, availability = get_mrp_sp_discount(soup)
                result["MRP"]          = mrp
                result["SP"]           = sp
                result["Discount %"]   = discount_pct
                result["Availability"] = availability

            # Brand
            if f("BRAND"):
                result["Brand"] = get_brand(soup)

            # Category (breadcrumb)
            if f("CATEGORY"):
                result["Category"] = get_category(soup)

            # Bullet points ("About this item")
            if f("BULLETS"):
                result["Bullet Points"] = get_bullets(soup)

            # Product description
            if f("DESCRIPTION"):
                result["Description"] = get_description(soup)

            # Fulfilled by (Amazon vs Seller)
            if f("FULFILLED_BY"):
                result["Fulfilled By"] = get_fulfilled_by(soup)

            # Stock count urgency text
            if f("STOCK_COUNT"):
                result["Stock Count Text"] = get_stock_count(soup)

            # Coupon
            if f("COUPON"):
                result["Coupon"] = get_coupon(soup)

            # Bank / EMI offers
            if f("BANK_OFFERS"):
                result["Bank Offers"] = get_bank_offers(soup)

            # Bestseller rank
            if f("BESTSELLER_RANK"):
                result["Bestseller Rank"] = get_bestseller_rank(soup)

            # Variation count
            if f("VARIATIONS"):
                result["Variation Count"] = get_variation_count(soup)

            # All live sellers — each gets own column pair
            if f("SELLERS"):
                print("    [SELLERS] Fetching for " + asin + "...")
                sellers_list = get_sellers_selenium(driver, asin)
                result["Total Live Sellers"] = len(sellers_list)

                if sellers_list:
                    for idx, s in enumerate(sellers_list):
                        result["Seller " + str(idx + 1)]         = s["seller"]
                        result["Seller " + str(idx + 1) + " Price"] = s["price"]
                    print("    [SELLERS] " + str(len(sellers_list)) + " found")
                else:
                    # Fallback: use buybox as Seller 1
                    result["Seller 1"]         = result["Buybox Seller"]
                    result["Seller 1 Price"]   = result["Buybox Price"]
                    result["Total Live Sellers"] = 1
                    print("    [SELLERS] Offer page unavailable, using buybox as Seller 1")

            # Images
            if f("IMAGES"):
                imgs = get_all_images(soup)
                result["Image URLs"] = "\n".join(imgs) if imgs else "Not Found"
                if imgs and nc12:
                    result["Images Downloaded"] = download_images(imgs, nc12, images_dir)
                elif imgs:
                    result["Images Downloaded"] = "No 12NC"

            return result

        except requests.exceptions.Timeout:
            print("  [TIMEOUT] " + asin + " attempt " + str(attempt))
            time.sleep(random.uniform(5, 10))
        except requests.exceptions.ConnectionError:
            print("  [CONNECTION ERROR] " + asin + " attempt " + str(attempt))
            time.sleep(random.uniform(8, 15))
        except Exception as e:
            print("  [ERROR] " + asin + ": " + str(e))
            time.sleep(random.uniform(3, 6))

    return result


# ═══════════════════════════════════════════════════════════════
#  UI-FIELD -> INTERNAL FLAG MAPPING  +  RUN LOOP
# ═══════════════════════════════════════════════════════════════
_UI_TO_FLAGS = {
    "Title": ["TITLE"],
    "Rating": ["RATING"],
    "Reviews": ["REVIEWS"],
    "Buybox Seller/Price": ["SOLD_BY"],
    "MRP/SP/Discount/Availability": ["MRP_SP"],
    "Brand": ["BRAND"],
    "Category": ["CATEGORY"],
    "Bullet Points": ["BULLETS"],
    "Description": ["DESCRIPTION"],
    "Fulfilled By": ["FULFILLED_BY"],
    "Stock Count": ["STOCK_COUNT"],
    "Coupon": ["COUPON"],
    "Bank Offers": ["BANK_OFFERS"],
    "Bestseller Rank": ["BESTSELLER_RANK"],
    "Variation Count": ["VARIATIONS"],
    "All Live Sellers (slow, needs browser)": ["SELLERS"],
    "Images": ["IMAGES"],
}


def _build_flags(ui_fields: dict) -> dict:
    flags = {}
    for ui_key, on in ui_fields.items():
        if not on:
            continue
        for internal_key in _UI_TO_FLAGS.get(ui_key, []):
            flags[internal_key] = True
    return flags


def scrape(asins, cfg, progress_cb=None, stop_flag=None):
    """
    asins      : list[str] raw ASINs or full amazon.in product URLs
    cfg        : {
        "fields": {ui_field_name: bool, ...}  (keys = DEFAULT_FIELDS),
        "nc12_map": {asin: "12NC"} (optional, enables per-ASIN image folders),
        "max_retries": 3, "save_every": 5,
        "images_dir": "amazon_images",
    }
    progress_cb: optional callable(idx, total, row_dict)
    stop_flag  : optional common.StopFlag()
    Returns list[dict] with columns from ALL_COLS (only requested ones populated).
    """
    ui_fields = cfg.get("fields", DEFAULT_FIELDS)
    flags = _build_flags(ui_fields)
    max_retries = cfg.get("max_retries", 3)
    images_dir = cfg.get("images_dir", "amazon_images")
    nc12_map = cfg.get("nc12_map", {})

    ids = [extract_id(a) for a in asins]

    session = requests.Session()
    try:
        session.get("https://www.amazon.in/", headers=get_headers(), timeout=10)
        time.sleep(random.uniform(1.5, 3))
    except Exception:
        pass

    driver = None
    if flags.get("SELLERS"):
        driver = make_chrome_driver(headless=True, window_size="1920,1080")

    results = []
    try:
        for idx, asin in enumerate(ids, 1):
            if stop_flag and stop_flag.stop:
                break

            nc12 = nc12_map.get(asin)
            data = scrape_asin(
                session, driver, asin, nc12=nc12,
                flags=flags, max_retries=max_retries, images_dir=images_dir,
            )
            results.append(data)
            if progress_cb:
                progress_cb(idx, len(ids), data)

            if idx % 5 == 0:
                time.sleep(random.uniform(8, 14))
            else:
                time.sleep(random.uniform(3, 6))
    finally:
        if driver:
            driver.quit()

    return results
