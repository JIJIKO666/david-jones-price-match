#!/usr/bin/env python3
"""
Compare David Jones and The Iconic prices for sale items.

Usage:
    # Using virtual environment (recommended):
    source venv/bin/activate
    python compare-davidjones-and-iconic.py [searchby] [threshold]

    # Or directly:
    ./venv/bin/python compare-davidjones-and-iconic.py [searchby] [threshold]

    searchby: The Iconic category to search (default: "womens-sale")
    threshold: Minimum discount required (default: 200)

Example:
    python compare-davidjones-and-iconic.py womens-sale 200
    python compare-davidjones-and-iconic.py misha-collection 100
"""

from urllib.parse import urljoin, urlencode, urlparse, parse_qsl, urlunparse
import pandas as pd
import re
import time
import requests
import json
import sys
from typing import List, Dict, Tuple, Optional, Any
import warnings
from bs4 import BeautifulSoup, MarkupResemblesLocatorWarning
from urllib.parse import quote

warnings.filterwarnings('ignore', category=MarkupResemblesLocatorWarning)

# Configuration
CHROME_HEADERS = {  # macOS Chrome UA
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/129.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


def http_request(url, base_headers=CHROME_HEADERS,
                 timeout=(5, 15), retries: int = 2, sleep: float = 8,
                 method: str = 'GET', json_data: dict = None, data: Any = None):
    """
    Unified HTTP request function supporting both GET and POST.
    """

    # meta-charset sniffing for HTML/XML
    _META_CHARSET_RE = re.compile(
        br"""(?ix)
        (?:<meta[^>]+charset=["']?\s*([a-z0-9_\-]+)\s*["']?[^>]*>)|
        (?:<meta[^>]+http-equiv=["']?content-type["']?[^>]+content=["'][^"']*;\s*charset=([a-z0-9_\-]+)[^"']*["'][^>]*>)|
        (?:^<\?xml[^>]*encoding=["']\s*([a-z0-9_\-]+)\s*["'])
        """
    )

    def _sniff_charset(raw_head: bytes) -> Optional[str]:
        m = _META_CHARSET_RE.search(raw_head)
        if not m:
            return None
        enc = next((g for g in m.groups() if g), None)
        if not enc:
            return None
        enc = enc.decode("ascii", "ignore").lower()
        if enc in ("gbk", "cp936"):
            return "gb18030"
        if enc == "utf8":
            return "utf-8"
        return enc

    def _attempt(verify: bool) -> Tuple[str, Dict[str, str]]:
        def auto_parse(text):
            try:
                if text.lstrip("\ufeff \t\r\n").startswith(("{", "[")):
                    return json.loads(text)
                if "html" in text[:1000].lower():
                    return BeautifulSoup(text, "html.parser")
                else:
                    return text
            except Exception as e:
                print(f'[ERROR] auto_parse (json or html) failed')

        # Prepare request kwargs
        req_kwargs = {
            'headers': base_headers,
            'timeout': timeout,
            'verify': verify
        }

        # Add POST data if provided
        if method.upper() == 'POST':
            if json_data is not None:
                req_kwargs['json'] = json_data
            elif data is not None:
                req_kwargs['data'] = data

        # Make request
        if method.upper() == 'POST':
            r = requests.post(url, **req_kwargs)
        else:
            r = requests.get(url, **req_kwargs)

        r.raise_for_status()  # raises on 400–599 -> requests.HTTPError

        heads = {k.lower(): v for k, v in r.headers.items()}
        ct = heads.get("content-type", "")

        # JSON is UTF-8 by spec; don't overthink it unless server lies badly
        if "application/json" in ct:
            r.encoding = r.encoding or "utf-8"
            return auto_parse(r.text), heads

        # otherwise (HTML/...), choose best encoding BEFORE using .text
        head_bytes = r.content[:32768]
        sniff = _sniff_charset(head_bytes)
        if sniff:
            r.encoding = sniff
        else:
            if not r.encoding or r.encoding.lower() in ("iso-8859-1", "ascii"):
                if getattr(r, "apparent_encoding", None):
                    r.encoding = r.apparent_encoding
                else:
                    r.encoding = "utf-8"

            if r.encoding.lower().startswith("utf-8"):
                hb = head_bytes.lower()
                if b"charset=gb" in hb or b"charset = gb" in hb:
                    r.encoding = "gb18030"

        return auto_parse(r.text), heads

    for _ in range(max(1, retries)):
        try:
            return _attempt(verify=True)
        except requests.exceptions.SSLError as e:
            # try with SSL verification disabled (public pages only!)
            try:
                return _attempt(verify=False)
            except Exception as e:
                pass
        except Exception as e:
            pass
        # retry
        time.sleep(sleep)

    raise RuntimeError(f'request failed for {url}')


def _turn_page(url: str, page: int) -> str:
    if page <= 1:
        return url
    u = urlparse(url)
    q = dict(parse_qsl(u.query, keep_blank_values=True))
    q["page"] = str(page)
    new_q = urlencode(q)
    return urlunparse((u.scheme, u.netloc, u.path, u.params, new_q, u.fragment))


def get_num(text: str):
    try:
        m = re.compile(r"-?\d{1,3}(?:,\d{3})*(?:\.\d+)?|-?\d+(?:\.\d+)?").search(text)
        num = m.group(0).replace(",", "")
        return float(num)
    except Exception:
        return None


def scrape_iconic(brand="misha-collection", threshold=0):
    """Scrape The Iconic for products."""
    start_url = f"https://www.theiconic.com.au/{brand}"

    rows = []
    page = 1

    while True:
        url = _turn_page(start_url, page)
        soup, _ = http_request(url)

        items = soup.select("a.product-details")
        if not items:
            break

        for a in items:

            price_final = get_num(a.select_one("span.price.final").get_text(" ", strip=True)
                                  if a.select_one("span.price.final") else None)
            price_original = get_num(a.select_one("span.price.original").get_text(" ", strip=True)
                                     if a.select_one("span.price.original") else None)

            if price_final and price_original:
                price_diff = price_original - price_final
                if price_diff >= threshold:

                    brand_text = (a.select_one("span.brand").get_text(strip=True)
                                  if a.select_one("span.brand") else "").strip()
                    name = (a.select_one("span.name").get_text(strip=True)
                            if a.select_one("span.name") else "").strip()
                    title = (f"{brand_text} {name}").strip()

                    href = a.get("href", "").strip()
                    link = urljoin("https://www.theiconic.com.au", href) if href else ""

                    rows.append({"title": title, "price": price_final, "was": price_original, "diff": price_diff, "link": link})

        page += 1

    df = pd.DataFrame(
        rows, columns=["title", "price", "was", "diff", "link"]
    ).sort_values("diff", ascending=False).reset_index(drop=True)

    return df


def get_product_info(item):
    brand_elem = item.select_one("p.ProductCard_brand__SYBe7")
    brand = brand_elem.get_text(strip=True) if brand_elem else ""
    name_elem = item.select_one("h2.ProductCard_name__p_7X2")
    name = name_elem.get_text(strip=True) if name_elem else ""
    title = f"{brand} {name}".strip()

    link = item.select_one("div.yotpo-widget-instance")['data-yotpo-url']
    link = urljoin("https://www.davidjones.com", link)    
    
    # Extract product ID from link for special offers API
    product_id = None
    id_match = re.search(r'-(\d+)(?:\?|$)', link)
    product_id = id_match.group(1)
    
    # Price extraction from Price_root__y8UOm
    price_root = item.select_one("div.Price_root__y8UOm")
    price_plain = None
    price_now = None
    
    # Check the accessibility text for price info
    # Pattern: "Price is now $220.00, it was $443.00" or "Price $399.00"
    accessibility_text = price_root.select_one("span[style*='position:absolute']")
    if accessibility_text:
        text = accessibility_text.get_text(strip=True)
        # Check if it's a sale price
        if "it was" in text.lower():
            # Pattern: "Price is now $220.00, it was $443.00"
            now_match = re.search(r'now\s+\$([0-9,]+\.?\d*)', text, re.IGNORECASE)
            was_match = re.search(r'was\s+\$([0-9,]+\.?\d*)', text, re.IGNORECASE)
            if now_match:
                price_now = float(now_match.group(1).replace(',', ''))
            if was_match:
                price_plain = float(was_match.group(1).replace(',', ''))
        else:
            # Pattern: "Price $399.00"
            price_match = re.search(r'Price\s+\$([0-9,]+\.?\d*)', text, re.IGNORECASE)
            if price_match:
                price_plain = float(price_match.group(1).replace(',', ''))
    
    # Determine final price and was price
    candidates = [p for p in (price_plain, price_now) if p is not None]
    price = min(candidates) if candidates else None
    was = max(candidates) if candidates else None
    
    return title, price, was, link, product_id


def apply_offer_discount(price_plain, price_now, offer_text):
    """Apply promotional offer discount to price."""
    if not offer_text:
        return None

    discount = get_num(offer_text)
    if not discount:
        return None

    price_offer = None

    if offer_text.startswith("EXTRA") and "%" in offer_text:
        if price_now:
            price_offer = round(price_now * (1 - discount / 100), 2)
        elif price_plain:
            price_offer = round(price_plain * (1 - discount / 100), 2)
    elif offer_text.startswith("SAVE") and "%" in offer_text:
        if price_plain:
            price_offer = round(price_plain * (1 - discount / 100), 2)
    elif offer_text.startswith("SAVE") and "$" in offer_text:
        if price_plain:
            price_offer = round(price_plain - discount, 2)
    elif offer_text.startswith("BUY"):
        m_buy = re.search(r'\bBUY\s+(\d+)\s+FOR\s*\$?\s*([0-9]+(?:\.[0-9]{1,2})?)\b', offer_text)
        if m_buy:
            qty = int(m_buy.group(1))
            total = float(m_buy.group(2))
            if qty > 0:
                price_offer = round(total / qty, 2)
    elif "GIFT CARD" in offer_text:
        if price_plain >= 600:
            price_offer = price_plain-150
        elif price_plain >= 300:
            price_offer = price_plain-50
        elif price_plain >= 150:
            price_offer = price_plain-20

    return price_offer


def _tokens(title: str):
    """Extract alphanumeric tokens from title."""
    if not isinstance(title, str):
        return set()
    return set(re.compile(r"[a-z0-9]+").findall(title.lower()))


def _jaccard(a: set, b: set) -> float:
    """Calculate Jaccard similarity between two sets."""
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def compare_search(pm, sim_thresh=0.9):
    """Search David Jones for each product from competitor and compare prices."""
    matches = []
    SEARCH_BASE = "https://www.davidjones.com/search?q="

    pms = pm.to_dict("records")
    total = len(pms)
    for idx, i in enumerate(pms, 1):
        print(f"*{idx}/{total}", flush=True)
        title_pm = i["title"]
        search_url = f"{SEARCH_BASE}{quote(title_pm)}"

        # Retry until we get SSR variant
        while True:
            try:
                soup, _ = http_request(search_url)
                time.sleep(1)
                break
            except Exception as e:
                print("[ERR] ", e)
                print("      title: ", title_pm)
                break

        # Select items from ul#products-grid > li
        items = soup.select("ul#products-grid > li")
        if not items:
            continue

        item = items[0]
        title, price, was, link, product_id = get_product_info(item)

        try:
            offers_url = "https://www.davidjones.com/routes/special-offers"
            offers_data, _ = http_request(offers_url, method='POST', json_data={"ids": [product_id]})

            offer_text = ""
            for offer in offers_data:
                if offer.get("id") == product_id:
                    offer_text = offer.get("shortDescription", "")
                    break

            if offer_text:
                # Apply offer discount
                price_offer = apply_offer_discount(was or price, price, offer_text)
                if price_offer:
                    candidates = [p for p in (was or price, price, price_offer) if p is not None]
                    price = min(candidates)
                    was = max(candidates)
        except Exception as e:
            print(f"[WARN] Failed to fetch special offer for {product_id}: {e}")

        if was == i["was"] and _jaccard(_tokens(title), _tokens(title_pm)) >= sim_thresh:

            price_pm = i["price"]
            price_diff = price - price_pm
            link_pm = i["link"]

            if price_diff > 100:
                matches.append({
                    "title_dj": title,
                    "title_pm": title_pm,
                    "price_diff": price_diff,
                    "price_dj": price,
                    "price_pm": price_pm,
                    "link_dj": link,
                    "link_pm": link_pm,
                })
            if price_diff > 250:
                print(f"[***]\n{title}\n{price} - {price_pm} = {price_diff}\ndj: {link}\npm: {link_pm}")
            elif price_diff > 200:
                print(f"[**]\n{title}\n{price} - {price_pm} = {price_diff}\ndj: {link}\npm: {link_pm}")
            elif price_diff > 150:
                print(f"[*]\n{title}\n{price} - {price_pm} = {price_diff}\ndj: {link}\npm: {link_pm}")

    compare_out = pd.DataFrame(
        matches,
        columns=["title_dj", "title_pm", "price_diff", "price_dj", "price_pm", "link_dj", "link_pm"]
    ).sort_values("price_diff", ascending=False).reset_index(drop=True)

    return compare_out


def main():
    """Main execution function."""
    # Parse command line arguments
    searchby = sys.argv[1] if len(sys.argv) > 1 else "womens-sale"
    threshold = int(sys.argv[2]) if len(sys.argv) > 2 else 200

    print(f"Scraping The Iconic: {searchby} (threshold: {threshold})")
    print("=" * 80)

    # Scrape The Iconic
    ic = scrape_iconic(searchby, threshold)
    print(ic.to_string(index=False, line_width=None))

    print("\n" + "=" * 80)
    print(f"Found {len(ic)} items from The Iconic")
    print("Now comparing with David Jones...")
    print("=" * 80 + "\n")

    # Compare with David Jones
    out = compare_search(ic)

    print("\n" + "=" * 80)
    print(f"Top 10 matches (total: {len(out)} matches):")
    print("=" * 80)
    print(out.head(10).to_string(index=False, line_width=None))


if __name__ == "__main__":
    main()
# pyenv shell 3.12.7
# python compare-davidjones-and-iconic.py
# python compare-davidjones-and-iconic.py mens-clothing-sale
# python compare-davidjones-and-iconic.py kids-sale
# python compare-davidjones-and-iconic.py sports-sale
# python compare-davidjones-and-iconic.py misha-collection 100