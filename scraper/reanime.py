"""Scraper for reanime.to / reanime.cz (Re:ANIME mirror site).

These are SSR React/Svelte sites that use AniList data for metadata.
The home page lists 24 anime cards; detail pages have rich metadata.
The ``/api/v1/anime`` endpoint exists but requires authentication (401).

Video URLs are hosted on a third-party CDN (flixcloud.cc).
The flixcloud pages load video dynamically via JavaScript (ArtPlayer),
so Playwright is required to capture the HLS (m3u8) stream URLs.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from bs4 import BeautifulSoup
from curl_cffi import requests as cffi_requests

BASE_URL = "https://reanime.to"
ALT_URL = "https://reanime.cz"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)


def session(impersonate: str = "chrome") -> cffi_requests.Session:
    s = cffi_requests.Session()
    s.headers.update(
        {
            "User-Agent": UA,
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
    )
    s.impersonate = impersonate
    return s


def _get(s, url: str, retries: int = 3, delay: float = 1.0) -> str:
    last = None
    for attempt in range(retries):
        r = s.get(url, timeout=30)
        if r.status_code == 200 and "Just a moment" not in r.text[:2000]:
            return r.text
        last = f"status={r.status_code}"
        time.sleep(delay * (attempt + 1))
    raise RuntimeError(f"Failed to fetch {url}: {last}")


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _resolve_url(href: str, base: str = BASE_URL) -> str:
    if href.startswith("http"):
        return href
    if href.startswith("/"):
        return f"{base}{href}"
    return f"{base}/{href}"


# --------------------------------------------------------------------------- #
# Home page
# --------------------------------------------------------------------------- #


def parse_homepage(html: str, base: str = BASE_URL) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")

    # Hero carousel: find the first anime slug from banner images
    hero_slug = ""
    for a in soup.find_all("a", href=True):
        m = re.match(r"/anime/([a-z0-9-]+)$", a["href"])
        if m:
            hero_slug = m.group(1)
            break

    # Anime cards across all sections
    anime_cards = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        m = re.match(r"/anime/([a-z0-9-]+)$", href)
        if not m or m.group(1) in seen:
            continue
        seen.add(m.group(1))
        img = a.find("img")
        title = a.get("title", "") or (img.get("alt", "") if img else "")
        anime_cards.append(
            {
                "slug": m.group(1),
                "url": _resolve_url(href),
                "title": title,
                "image": img["src"] if img and img.get("src") else "",
            }
        )

    # Sections
    sections: dict[str, list[str]] = {}
    current = ""
    for el in soup.find_all(["h2", "h3"]):
        txt = el.get_text(strip=True)
        if txt and txt not in ("Related Seasons & Series", "Stats", "Studios"):
            current = txt
            sections.setdefault(current, [])

    # Map cards to sections (heuristic: cards after an h2 belong to that section)
    # Instead, just return flat list and section headings
    return {
        "base": base,
        "hero_slug": hero_slug,
        "anime_cards": anime_cards,
        "sections": list(sections.keys()),
        "total_anime_on_page": len(anime_cards),
    }


def scrape_homepage(base: str = BASE_URL, s=None) -> dict[str, Any]:
    s = s or session()
    return parse_homepage(_get(s, f"{base}/home"), base=base)


# --------------------------------------------------------------------------- #
# Anime detail
# --------------------------------------------------------------------------- #


def parse_anime_detail(html: str, slug: str, base: str = BASE_URL) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")

    # Titles
    h1 = soup.find("h1")
    title_en = h1.get_text(strip=True) if h1 else ""

    h2 = soup.find("h2")
    title_jp = h2.get_text(strip=True) if h2 else ""

    # Images
    banner = ""
    cover = ""
    for img in soup.find_all("img", src=True):
        src = img["src"]
        if "/anime/banner/" in src and not banner:
            banner = src
        elif "/anime/cover/" in src and not cover:
            cover = src
        elif "anilistcdn" in src and "/banner/" in src and not banner:
            banner = src
        elif "anilistcdn" in src and "/cover/" in src and not cover:
            cover = src

    # Meta info: look for the sidebar/desk stats section
    # The page has spans with labels like "Episodes", "Duration", "Status", "Start Date", "Season"
    meta: dict[str, str] = {}
    labels = soup.find_all("div", class_=re.compile(r"flex flex-col gap-1"))
    for label_div in labels:
        spans = label_div.find_all("span")
        if len(spans) >= 2:
            key = _clean(spans[0].get_text(" ", strip=True))
            val = _clean(spans[1].get_text(" ", strip=True))
            if key and val:
                meta[key] = val

    # Alternative: look for the stats grid (grid-cols-2 / grid-cols-4)
    stats_grid = soup.find("div", class_=re.compile(r"grid-cols-2|grid-cols-4"))
    if stats_grid:
        for div in stats_grid.find_all("div", class_=re.compile(r"flex flex-col")):
            spans = div.find_all("span")
            if len(spans) >= 2:
                key = _clean(spans[0].get_text(" ", strip=True))
                val = _clean(spans[1].get_text(" ", strip=True))
                if key and val:
                    meta[key] = val

    # Type, episodes, duration, status, start_date, season, year
    # These are also in the mobile stats section
    # Extract from the text directly
    for span in soup.find_all("span", class_=re.compile(r"text-xs|text-sm|font-medium")):
        txt = _clean(span.get_text(" ", strip=True))
        if not txt:
            continue
        # "TV 25 episodes 24 min Finished Spring 2006"
        m = re.match(
            r"(TV|OVA|SPECIAL|MOVIE|WEB|Music|Other)\s+(\d+)\s+episodes?\s+(\d+)\s*min?\s+(Finished|Ongoing|Upcoming|Cancelled|Publishing)\s+(\w+)\s+(\d{4})",
            txt,
        )
        if m:
            meta["type"] = m.group(1)
            meta["episodes"] = m.group(2)
            meta["duration"] = f"{m.group(3)} min"
            meta["status"] = m.group(4)
            meta["season"] = m.group(5)
            meta["year"] = m.group(6)

    # Synopsis
    synopsis = ""
    for div in soup.find_all("div", class_=re.compile(r"overflow-hidden|relative.*max-h|transition.*max-height")):
        txt = _clean(div.get_text(" ", strip=True))
        if len(txt) > 50 and "Read More" not in txt[:20]:
            synopsis = txt
            break
    # Fallback: look for "Read More" text and get the preceding content
    if not synopsis:
        for el in soup.find_all(string=re.compile(r"Read More")):
            parent = el.parent
            if parent:
                synopsis = _clean(parent.get_text(" ", strip=True).replace("Read More", "").strip())
                break

    # Tags
    tags = []
    tag_container = soup.find("div", class_=re.compile(r"flex flex-wrap.*gap-2"))
    if tag_container:
        for a in tag_container.find_all("a", href=True):
            href = a["href"]
            m = re.search(r"/anime/[^/]+$", href)
            # Tags are usually just text spans, not links, in a flex-wrap div
            txt = _clean(a.get_text(strip=True))
            if txt and len(txt) < 30 and txt not in (title_en, title_jp):
                tags.append(txt)
    # Also look for genre tags in the text
    if not tags:
        for span in soup.find_all("span", class_=re.compile(r"text-xs|text-sm|font-medium|px-2|py-1|rounded")):
            txt = _clean(span.get_text(strip=True))
            if txt and txt not in (title_en, title_jp, "Add to List", "Watch Now", "Share", "TV", "OVA", "SPECIAL", "MOVIE", "WEB") and len(txt) < 30:
                # Check if it looks like a tag (single word or short phrase)
                if re.match(r"^[A-Z][a-z]+(\s+[A-Z][a-z]+)*$", txt):
                    tags.append(txt)

    # Studios
    studios = []
    studios_heading = soup.find(string=re.compile(r"^Studios\s*$"))
    if studios_heading:
        next_div = studios_heading.parent.find_next_sibling("div")
        if next_div:
            studios = [a.get_text(strip=True) for a in next_div.find_all("a") if a.get_text(strip=True)]

    # Stats grid: each child div has combined label+value text like "Episodes25"
    stats_grid = soup.find("div", class_=re.compile(r"grid-cols-2.*gap-4"))
    if stats_grid:
        for child in stats_grid.find_all(recursive=False):
            txt = _clean(child.get_text(strip=True))
            m = re.match(r"^(Episodes|Duration|Subbed|Dubbed|Rating|Score|Popularity|Members|Favorites|Type|Status|Start\s+Date|Season)\s*(.*)$", txt, re.I)
            if m:
                meta[m.group(1).replace(" ", "")] = m.group(2).strip()

    # Type, status, season, year from the sidebar labels section
    sidebar = soup.find(string=re.compile(r"Type\s+ANIME"))
    if sidebar:
        txt = _clean(sidebar.parent.get_text(" ", strip=True))
        m = re.search(
            r"Type\s+(TV|OVA|SPECIAL|MOVIE|WEB|Music|Other)\s+.*?Episodes\s+(\d+).*?Duration\s+(\d+)\s*min.*?Status\s+(\w+).*?Start\s+Date\s+(\S+).*?Season\s+(\w+)\s+(\d{4})",
            txt,
        )
        if m:
            meta["type"] = m.group(1)
            meta["episodes"] = m.group(2)
            meta["duration"] = f"{m.group(3)} min"
            meta["status"] = m.group(4)
            meta["start_date"] = m.group(5)
            meta["season"] = m.group(6)
            meta["year"] = m.group(7)

    # Related seasons/series - target the specific container
    related = []
    related_heading = soup.find(string=re.compile(r"RELATED SEASONS"))
    if related_heading:
        container = related_heading.parent.find_parent("div", class_=re.compile(r"mt-6|border-t|pt-6"))
        if container:
            for a in container.find_all("a", href=True):
                href = a["href"]
                m = re.match(r"/anime/([a-z0-9-]+)$", href)
                if m:
                    txt = _clean(a.get_text(" ", strip=True))
                    if txt and txt not in ("Previous slide", "Next slide", "3 ENTRIES") and len(txt) < 120 and m.group(1) != slug:
                        related.append({"slug": m.group(1), "url": f"{base}/anime/{m.group(1)}", "title": txt})

    # Deduplicate related
    seen_related = set()
    unique_related = []
    for r in related:
        if r["slug"] not in seen_related:
            seen_related.add(r["slug"])
            unique_related.append(r)

    # Extract slug from URL if not provided
    # Get the page's canonical or og:url
    og_url = ""
    for meta_tag in soup.find_all("meta"):
        if meta_tag.get("property") == "og:url" or meta_tag.get("name") == "url":
            og_url = meta_tag.get("content", "")
            break

    return {
        "slug": slug,
        "url": f"{base}/anime/{slug}",
        "title_en": title_en,
        "title_jp": title_jp,
        "title": title_en or title_jp,
        "banner": banner,
        "cover": cover,
        "meta": meta,
        "synopsis": synopsis,
        "tags": tags,
        "studios": studios,
        "related": unique_related[:10],
        "og_url": og_url,
    }


def scrape_anime(slug: str, base: str = BASE_URL, s=None) -> dict[str, Any]:
    s = s or session()
    html = _get(s, f"{base}/anime/{slug}")
    return parse_anime_detail(html, slug, base=base)


# --------------------------------------------------------------------------- #
# Video URL scraping (flixcloud.cc / Playwright)
# --------------------------------------------------------------------------- #


def _get_server_data(anilist_id: int, episode: int, base: str = BASE_URL, s=None) -> dict[str, Any]:
    """Fetch server data from the reanime.to flix API."""
    s = s or session()
    url = f"{base}/api/flix/{anilist_id}/{episode}"
    resp = s.get(url, timeout=15)
    if resp.status_code != 200:
        raise RuntimeError(f"Failed to fetch server data from {url}: status={resp.status_code}")
    return json.loads(resp.text)


def _extract_flixcloud_url(server_data: dict[str, Any], server_name: str = "HD-1", data_type: str = "sub") -> Optional[str]:
    """Extract the flixcloud.cc embed URL from server data."""
    for s in server_data.get("servers", []):
        if s.get("serverName") == server_name and s.get("dataType") == data_type:
            return s.get("dataLink")
    # Fallback: return first server URL
    servers = server_data.get("servers", [])
    if servers:
        return servers[0].get("dataLink")
    return None


def scrape_video_urls(
    anilist_id: int,
    episode: int,
    base: str = BASE_URL,
    server_name: str = "HD-1",
    data_type: str = "sub",
    timeout: int = 30,
) -> dict[str, Any]:
    """Scrape HLS (m3u8) video URLs for a given anime episode.

    Uses the reanime.to API to get the flixcloud.cc embed URL,
    then Playwright to render the flixcloud page and capture
    the m3u8 stream URLs from network requests.

    Returns a dict with the server data and captured m3u8 URLs.
    """
    from playwright.sync_api import sync_playwright

    # Step 1: Get server data from reanime.to API
    server_data = _get_server_data(anilist_id, episode, base=base)

    # Step 2: Extract the flixcloud embed URL
    flixcloud_url = _extract_flixcloud_url(server_data, server_name=server_name, data_type=data_type)
    if not flixcloud_url:
        return {
            "anilist_id": anilist_id,
            "episode": episode,
            "server_data": server_data,
            "flixcloud_url": None,
            "m3u8_urls": {},
            "error": "No flixcloud URL found in server data",
        }

    # Step 3: Use Playwright to render the flixcloud page and capture m3u8 URLs
    m3u8_urls: dict[str, str] = {}

    def handle_request(request):
        url = request.url
        if ".m3u8" in url and "flixcloud" in url:
            # Capture the m3u8 URL by type (master, video, audio)
            if "master" in url:
                m3u8_urls["master"] = url
            elif "audio" in url or "native" in url:
                m3u8_urls["audio"] = url
            elif "video" in url:
                m3u8_urls["video"] = url

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=UA,
                viewport={"width": 1280, "height": 720},
            )
            page = context.new_page()

            # Intercept network requests for m3u8 URLs
            page.on("request", handle_request)

            # Navigate to the flixcloud embed page
            page.goto(flixcloud_url, wait_until="networkidle", timeout=timeout * 1000)

            # Wait a bit for the player to initialize and fetch the m3u8 URLs
            page.wait_for_timeout(3000)

            # Also try to extract video_id and aid from the page HTML
            page_content = page.content()
            video_id_match = re.search(r'video_id["\s:]+["\']([^"\']+)["\']', page_content)
            aid_match = re.search(r'aid["\s:]+["\']([^"\']+)["\']', page_content)

            browser.close()
    except Exception as e:
        return {
            "anilist_id": anilist_id,
            "episode": episode,
            "server_data": server_data,
            "flixcloud_url": flixcloud_url,
            "m3u8_urls": m3u8_urls,
            "error": str(e),
        }

    return {
        "anilist_id": anilist_id,
        "episode": episode,
        "server_data": server_data,
        "flixcloud_url": flixcloud_url,
        "video_id": video_id_match.group(1) if video_id_match else "",
        "aid": aid_match.group(1) if aid_match else "",
        "m3u8_urls": m3u8_urls,
    }


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


def run(
    *,
    base: str = BASE_URL,
    limit: int = 24,
    fetch_details: bool = True,
    delay: float = 0.3,
) -> dict[str, Any]:
    s = session()
    home = scrape_homepage(base=base, s=s)
    cards = home["anime_cards"][:limit]

    results = []
    for i, card in enumerate(cards, 1):
        print(f"[{i}/{len(cards)}] {card['title'][:60]}")
        if fetch_details:
            try:
                detail = scrape_anime(card["slug"], base=base, s=s)
            except Exception as e:
                detail = {"slug": card["slug"], "error": str(e)}
            results.append(detail)
        else:
            results.append(card)
        time.sleep(delay)

    return {
        "scraped_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "base": base,
        "home": home,
        "anime": results,
    }