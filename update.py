#!/usr/bin/env python3
"""Rebuild the Greece news-website list.

Pulls free public sources, keeps country = Greece, and ranks outlets with
the latest Tranco top-1M list. Designed to run on the 1st of each month.
Uses only the Python standard library.
"""

from __future__ import annotations

import csv
import io
import re
import sys
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
SEED_PATH = ROOT / "seeds" / "known-greece-news.csv"
OUT_PATH = ROOT / "greece-news-sites.csv"
ARCHIVE_DIR = ROOT / "archive"
LOG_PATH = ROOT / "update.log"

USER_AGENT = "greece-news-list/1.0 (local monthly Greece news list)"
TIMEOUT = 60

# Small queries. Wikidata is rate-limiting heavier SPARQL during an outage.
WIKIDATA_QUERIES = [
    """
    SELECT DISTINCT ?item ?itemLabel ?website WHERE {
      ?item wdt:P31 wd:Q11032 ; wdt:P17 wd:Q41 ; wdt:P856 ?website .
      SERVICE wikibase:label { bd:serviceParam wikibase:language "en,el". }
    }
    """,
    """
    SELECT DISTINCT ?item ?itemLabel ?website WHERE {
      ?item wdt:P31 wd:Q1153191 ; wdt:P17 wd:Q41 ; wdt:P856 ?website .
      SERVICE wikibase:label { bd:serviceParam wikibase:language "en,el". }
    }
    """,
    """
    SELECT DISTINCT ?item ?itemLabel ?website WHERE {
      ?item wdt:P31 wd:Q17232649 ; wdt:P17 wd:Q41 ; wdt:P856 ?website .
      SERVICE wikibase:label { bd:serviceParam wikibase:language "en,el". }
    }
    """,
]

# Not news publishers. Directories mix these into "news" pages.
SKIP_DOMAINS = {
    "weather.gr",
    "k24.net",
    "akous.gr",
    "athensparty.com",
    "demokritos.gr",
    "atticaradios.gr",
    "mad.tv",
    "yahoo.com",
    "google.com",
    "youtube.com",
    "instagram.com",
    "facebook.com",
    "wikipedia.org",
    "twitter.com",
    "x.com",
    "medium.com",
    "wordpress.com",
    "blogspot.com",
    "protoselidaefimeridon.gr",
    "topgeargreece.gr",
    "eviathema.eu",
    "eviathema.info",
    "eviathema.news",
    "eviathema.online",
    "eviathema.world",
}

# Diaspora and Cyprus titles that show up on Greek-language lists.
SKIP_SUFFIXES = (".cy", ".com.cy")
SKIP_EXTRA = {
    "greekreporter.com",
    "thenationalherald.com",
    "greeknewsonline.com",
    "eleftheria.co.uk",
    "24h.com.cy",
    "24sports.com.cy",
    "actioninsports.com",
    "ant1.com.cy",
    "cyprusnews.eu",
    "cytoday.eu",
    "omegalive.com.cy",
    "omonoianews.com",
    "pafospress.com",
    "paideia-news.com",
    "philenews.com",
    "politis.com.cy",
    "sigmalive.com",
    "sportscy.net",
    "typos.com.cy",
    "kathimerini.com.cy",
}


def log(message: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"{stamp} {message}"
    print(line)
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def fetch(url: str, accept: str | None = None, browser: bool = False) -> bytes:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
            if browser
            else USER_AGENT
        )
    }
    if accept:
        headers["Accept"] = accept
    request = Request(url, headers=headers)
    with urlopen(request, timeout=TIMEOUT) as response:
        return response.read()


def host_of(value: str) -> str:
    text = (value or "").strip()
    if not text or any(ord(ch) > 127 for ch in text):
        return ""
    if "://" not in text:
        text = "http://" + text
    host = (urlparse(text).hostname or "").lower().strip(".")
    if host.startswith("www."):
        host = host[4:]
    if not host or "." not in host or any(ord(ch) > 127 for ch in host):
        return ""
    return host


INFRA_SUFFIXES = (
    "cloudflare.com",
    "googleapis.com",
    "gstatic.com",
    "wordpress.org",
    "w.org",
    "w3.org",
    "amcharts.com",
    "googletagmanager.com",
    "schema.org",
    "gravatar.com",
    "github.com",
    "jsdelivr.net",
)


def skipped(domain: str) -> bool:
    if domain in SKIP_DOMAINS or domain in SKIP_EXTRA:
        return True
    if domain.endswith(SKIP_SUFFIXES) or domain.endswith(INFRA_SUFFIXES):
        return True
    return False


class Site:
    def __init__(self, domain: str, name: str, source: str) -> None:
        self.domain = domain
        self.name = name.strip()
        self.sources: set[str] = {source} if source else set()
        self.tranco_rank: int | None = None
        self.similarweb_rank: int | None = None
        self.search_rank: int | None = None

    def add(self, name: str, source: str) -> None:
        if source:
            self.sources.add(source)
        cleaned = (name or "").strip()
        if cleaned and not re.fullmatch(r"Q\d+", cleaned):
            if not self.name or re.fullmatch(r"Q\d+", self.name) or self.name == self.domain:
                self.name = cleaned


def add_site(sites: dict[str, Site], domain: str, name: str, source: str) -> None:
    domain = host_of(domain) if "://" in domain or domain.startswith("www.") else domain.strip().lower()
    if domain.startswith("www."):
        domain = domain[4:]
    if not domain or skipped(domain) or "." not in domain:
        return
    if any(ord(ch) > 127 for ch in domain):
        return
    current = sites.get(domain)
    if current is None:
        sites[domain] = Site(domain, name or domain, source)
    else:
        current.add(name, source)


def load_seed(sites: dict[str, Site]) -> None:
    if not SEED_PATH.exists():
        log(f"seed missing: {SEED_PATH}")
        return
    with SEED_PATH.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            add_site(sites, row.get("domain", ""), row.get("name", ""), "seed")
    log(f"seed rows kept so far: {len(sites)}")


def load_wikidata(sites: dict[str, Site]) -> None:
    added = 0
    for index, query in enumerate(WIKIDATA_QUERIES, start=1):
        url = "https://query.wikidata.org/sparql?" + urlencode({"query": query})
        raw = None
        for attempt in (1, 2, 3):
            try:
                raw = fetch(url, accept="text/csv").decode("utf-8")
                break
            except HTTPError as exc:
                if exc.code == 429 and attempt < 3:
                    log("wikidata rate limit; waiting 75s")
                    time.sleep(75)
                    continue
                raise
        if raw is None:
            continue
        for row in csv.DictReader(io.StringIO(raw)):
            before = len(sites)
            add_site(sites, row.get("website", ""), row.get("itemLabel", ""), "wikidata")
            if len(sites) > before:
                added += 1
        if index != len(WIKIDATA_QUERIES):
            time.sleep(5)
    log(f"wikidata new domains: {added}; sites now {len(sites)}")


def load_awesome(sites: dict[str, Site]) -> None:
    url = "https://raw.githubusercontent.com/divkakwani/awesome-newspapers/main/newspapers/el.csv"
    raw = fetch(url).decode("utf-8")
    for row in csv.DictReader(io.StringIO(raw)):
        if (row.get("country") or "").strip().lower() != "grc":
            continue
        add_site(sites, row.get("homeurl", ""), row.get("name", ""), "awesome-newspapers")
    log(f"after awesome-newspapers: {len(sites)}")


def load_w3(sites: dict[str, Site]) -> None:
    html = fetch("https://www.w3newspapers.com/greece/", browser=True).decode("utf-8", "replace")
    for href in re.findall(r'href="(https?://[^"]+)"', html):
        host = host_of(href)
        if not host or "w3newspapers.com" in host:
            continue
        add_site(sites, host, "", "w3newspapers")
    log(f"after w3newspapers: {len(sites)}")


def load_gov_capital(sites: dict[str, Site]) -> None:
    html = fetch("https://gov.capital/toplist/category/news/country/gr").decode("utf-8", "replace")
    hosts = re.findall(r'https?://(?:www\.)?([a-z0-9.-]+\.[a-z]{2,})', html, flags=re.I)
    blocked = ("gov.capital", "walletinvestor", "piperic", "cloudflare", "w3.org", "googleapis", "amcharts", "googletagmanager")
    for host in hosts:
        host = host.lower()
        if host.startswith("www."):
            host = host[4:]
        if any(part in host for part in blocked):
            continue
        add_site(sites, host, "", "gov.capital")
    log(f"after gov.capital: {len(sites)}")


def load_similarweb(sites: dict[str, Site]) -> None:
    html = fetch("https://www.similarweb.com/top-websites/greece/news-and-media/").decode("utf-8", "replace")
    domains = re.findall(r'"domain"\s*:\s*"([^"]+)"', html)
    seen: list[str] = []
    for domain in domains:
        domain = domain.lower().strip()
        if domain.startswith("www."):
            domain = domain[4:]
        if domain and domain not in seen:
            seen.append(domain)
    for index, domain in enumerate(seen, start=1):
        add_site(sites, domain, "", "similarweb")
        if domain in sites:
            sites[domain].similarweb_rank = index
    log(f"similarweb Greece news rows: {len(seen)}")


def load_ahrefs(sites: dict[str, Site]) -> None:
    html = fetch("https://ahrefstop.com/websites/greece/news").decode("utf-8", "replace")
    cut = re.search(r"Top Trending Websites|Trending up", html)
    if cut:
        html = html[: cut.start()]
    domains = re.findall(r'favicons\?domain=([a-z0-9.-]+)', html, flags=re.I)
    seen: list[str] = []
    for domain in domains:
        domain = domain.lower().strip()
        if domain.startswith("www."):
            domain = domain[4:]
        if not domain or domain in seen or domain.endswith("ahrefs.com") or domain in {"google.com", "firehose.com"}:
            continue
        seen.append(domain)
    for index, domain in enumerate(seen, start=1):
        add_site(sites, domain, "", "ahrefstop")
        if domain in sites and sites[domain].search_rank is None:
            sites[domain].search_rank = index
    log(f"ahrefstop Greece news rows: {len(seen)}")


def load_tranco(sites: dict[str, Site]) -> str:
    payload = fetch("https://tranco-list.eu/top-1m.csv.zip")
    list_id = fetch("https://tranco-list.eu/top-1m-id").decode("utf-8").strip()
    wanted = set(sites)
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        with archive.open("top-1m.csv") as handle:
            for line in handle:
                rank_text, domain = line.decode().rstrip("\r\n").split(",", 1)
                if domain in wanted and sites[domain].tranco_rank is None:
                    sites[domain].tranco_rank = int(rank_text)
    ranked = sum(1 for site in sites.values() if site.tranco_rank is not None)
    log(f"tranco list {list_id}; ranked {ranked} of {len(sites)}")
    return list_id


def sort_key(site: Site) -> tuple:
    return (
        site.similarweb_rank if site.similarweb_rank is not None else 10_000,
        site.search_rank if site.search_rank is not None else 10_000,
        site.tranco_rank if site.tranco_rank is not None else 10_000_000,
        site.domain,
    )


def write_output(sites: dict[str, Site], list_id: str) -> None:
    ordered = sorted(sites.values(), key=sort_key)
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    archive_path = ARCHIVE_DIR / f"{month}.csv"
    fieldnames = [
        "news_rank",
        "country",
        "domain",
        "name",
        "similarweb_greece_rank",
        "search_rank_greece",
        "tranco_rank",
        "sources",
        "generated",
        "tranco_list_id",
    ]
    rows = []
    for index, site in enumerate(ordered, start=1):
        rows.append(
            {
                "news_rank": index,
                "country": "Greece",
                "domain": site.domain,
                "name": site.name or site.domain,
                "similarweb_greece_rank": site.similarweb_rank or "",
                "search_rank_greece": site.search_rank or "",
                "tranco_rank": site.tranco_rank or "",
                "sources": ";".join(sorted(site.sources)),
                "generated": generated,
                "tranco_list_id": list_id,
            }
        )
    for path in (OUT_PATH, archive_path):
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
    log(f"wrote {len(rows)} rows to {OUT_PATH.name} and {archive_path.name}")


def main() -> int:
    sites: dict[str, Site] = {}
    steps = [
        ("seed", load_seed),
        ("wikidata", load_wikidata),
        ("awesome-newspapers", load_awesome),
        ("w3newspapers", load_w3),
        ("gov.capital", load_gov_capital),
        ("similarweb", load_similarweb),
        ("ahrefstop", load_ahrefs),
    ]
    for name, step in steps:
        try:
            step(sites)
        except (HTTPError, URLError, TimeoutError, OSError, csv.Error, ValueError) as exc:
            log(f"WARNING {name} failed: {exc}")
    if not sites:
        log("no sites collected; leaving the previous file in place")
        return 1
    try:
        list_id = load_tranco(sites)
    except (HTTPError, URLError, TimeoutError, OSError, zipfile.BadZipFile, ValueError, KeyError) as exc:
        log(f"WARNING tranco failed: {exc}")
        list_id = ""
    write_output(sites, list_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
