#!/usr/bin/env python3
"""
watcher.py

Searches Reddit's public RSS/Atom feeds for a watch term and writes matches.json
for the static site.

Why RSS: as of Reddit's Responsible Builder Policy (updated June 2026), the JSON
Data API requires an approved app. The public .rss endpoints need no app, no
client ID and no approval.

Trade-off: RSS entries carry no score, so "min_score" in config.json is ignored
(a warning is printed if you set it above 0). Everything else — title, body
excerpt, subreddit, author, timestamp, permalink — is present, so matches.json
keeps exactly the same shape and croissant.html needs no changes.

Behaviour notes:
- If every feed request fails, matches.json is left untouched and the script
  exits non-zero, so a broken run shows up red in Actions instead of silently
  publishing an empty list.
- Falls back from www.reddit.com to old.reddit.com if the first host blocks it.
- Loose (stem-aware) term matching by default: "bake" matches "baking",
  "baker", "baked". Set "match_mode" to "strict" or "off" to change that.

Usage:
  python watcher.py
  python watcher.py --debug
  WATCH_TERM="your term" python watcher.py --debug
"""

import argparse
import calendar
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urlencode

import feedparser
import requests

ROOT = os.getcwd()
CONFIG_PATH = os.path.join(ROOT, "config.json")
STATE_PATH = os.path.join(ROOT, "state.json")
MATCHES_PATH = os.path.join(ROOT, "matches.json")

DEFAULT_CONFIG = {
    "term": "bake croissants",
    "global_search": True,
    "subreddits": ["baking", "AskCulinary", "breadit"],
    "min_score": 0,           # ignored: RSS carries no score
    "max_age_hours": 168,
    "results_limit_per_query": 50,
    "match_mode": "loose",    # loose | strict | off
    "user_agent": "CroissantWatcher/1.0 (by u/pugBiz)",
}

# Tried in order; the first host that answers wins.
HOSTS = ["https://www.reddit.com", "https://old.reddit.com"]

MAX_KEEP = 200


# ---------------------------------------------------------------- utilities

def load_json(path, default=None):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"WARNING: failed to parse {path}: {e}")
            return default
    return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def now_ts():
    return int(time.time())


def iso8601(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def load_config():
    cfg = load_json(CONFIG_PATH, {})
    merged = DEFAULT_CONFIG.copy()
    if isinstance(cfg, dict):
        merged.update(cfg)
    return merged


def load_state():
    s = load_json(STATE_PATH, {})
    if not isinstance(s, dict):
        s = {}
    s.setdefault("seen_ids", [])
    return s


def quote_if_needed(term):
    if not term:
        return term
    t = term.strip()
    if " " not in t:
        return t
    if (t.startswith('"') and t.endswith('"')) or (t.startswith("'") and t.endswith("'")):
        return t
    return f'"{t}"'


# ---------------------------------------------------------------- matching

WORD_RE = re.compile(r"[a-z0-9']+")


def term_matches(text, term, mode):
    """strict = substring, loose = stem-aware, off = accept everything."""
    if mode == "off" or not term:
        return True

    t = term.lower().strip()
    text = (text or "").lower()

    if t in text:
        return True
    if mode == "strict":
        return False

    words = WORD_RE.findall(t)
    if not words:
        return False

    tokens = WORD_RE.findall(text)
    for w in words:
        stem = w[:-1] if len(w) > 3 and w.endswith("e") else w
        if len(stem) < 3:
            if w not in tokens:
                return False
            continue
        if not any(tok.startswith(stem) for tok in tokens):
            return False
    return True


def filter_posts(posts, term, cfg, debug=False):
    mode = (cfg.get("match_mode") or "loose").lower()
    max_age_hours = int(cfg.get("max_age_hours") or 168)
    cutoff_ts = now_ts() - int(max_age_hours * 3600)

    out = []
    dropped = {"no_id": 0, "age": 0, "term": 0}
    for p in posts:
        if not p.get("id"):
            dropped["no_id"] += 1
            continue
        created = int(p.get("created_utc") or 0)
        if created and created < cutoff_ts:
            dropped["age"] += 1
            continue
        haystack = " ".join([
            p.get("title") or "",
            p.get("excerpt") or "",
            p.get("subreddit") or "",
        ])
        if not term_matches(haystack, term, mode):
            dropped["term"] += 1
            continue
        p["found_at"] = iso8601(now_ts())
        out.append(p)

    if debug:
        print(f"[debug] dropped: {dropped} (match_mode={mode})")
    return out


# ---------------------------------------------------------------- feed parsing

TAG_RE = re.compile(r"<[^>]+>")
# Reddit appends a "submitted by /u/x to r/y" footer to every RSS entry body.
FOOTER_RE = re.compile(r"submitted by\s*/u/\S+.*$", re.IGNORECASE | re.DOTALL)


def strip_html(raw):
    if not raw:
        return ""
    text = TAG_RE.sub(" ", raw)
    text = html.unescape(text)
    text = FOOTER_RE.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def entry_id(entry):
    """Reddit Atom ids look like 't3_1abcdef'. Return the bare post id."""
    raw = entry.get("id") or ""
    m = re.search(r"t3_([a-z0-9]+)", raw, re.IGNORECASE)
    if m:
        return m.group(1)
    # Fallback: pull it out of the permalink /r/sub/comments/<id>/slug/
    m = re.search(r"/comments/([a-z0-9]+)", entry.get("link") or "", re.IGNORECASE)
    return m.group(1) if m else None


def entry_subreddit(entry):
    for tag in entry.get("tags") or []:
        term = tag.get("term")
        if term:
            return term.replace("r/", "")
    m = re.search(r"/r/([^/]+)/", entry.get("link") or "")
    return m.group(1) if m else None


def entry_created(entry):
    for key in ("published_parsed", "updated_parsed"):
        parsed = entry.get(key)
        if parsed:
            return calendar.timegm(parsed)  # feedparser returns UTC struct_time
    return 0


def entry_author(entry):
    author = entry.get("author") or ""
    return author.replace("/u/", "").strip() or None


def parse_feed(feed_bytes):
    parsed = feedparser.parse(feed_bytes)
    posts = []
    for e in parsed.entries:
        body = ""
        if e.get("content"):
            body = e["content"][0].get("value", "")
        elif e.get("summary"):
            body = e["summary"]

        posts.append({
            "id": entry_id(e),
            "fullname": e.get("id"),
            "title": html.unescape(e.get("title") or ""),
            "url": e.get("link"),
            "excerpt": strip_html(body)[:500],
            "subreddit": entry_subreddit(e),
            "author": entry_author(e),
            "score": None,          # not available over RSS
            "created_utc": entry_created(e),
            "type": "post",
        })
    return posts


# ---------------------------------------------------------------- fetching

class FeedFetcher:
    def __init__(self, user_agent, debug=False):
        self.debug = debug
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": user_agent,
            "Accept": "application/atom+xml, application/rss+xml, */*",
        })
        self.host = None  # locked in after the first success

    def get(self, path, params, attempts=3):
        hosts = [self.host] if self.host else HOSTS
        last_error = None

        for host in hosts:
            url = f"{host}{path}?{urlencode(params)}"
            for i in range(attempts):
                if self.debug:
                    print(f"[debug] GET {url} (attempt {i + 1})")
                try:
                    r = self.session.get(url, timeout=20)
                except requests.RequestException as e:
                    last_error = f"{host}: network error: {e}"
                    time.sleep(2 ** i)
                    continue

                if r.status_code == 200:
                    if b"<feed" not in r.content[:2000] and b"<rss" not in r.content[:2000]:
                        last_error = f"{host}: HTTP 200 but body was not a feed"
                        break
                    self.host = host
                    return r.content

                if r.status_code in (429, 500, 502, 503, 504):
                    last_error = f"{host}: HTTP {r.status_code}"
                    time.sleep(2 ** i * 2)
                    continue

                last_error = f"{host}: HTTP {r.status_code}"
                break

        print(f"ERROR: feed request failed - {last_error}")
        return None

    def search_sitewide(self, query, limit):
        return self.get("/search.rss",
                        {"q": query, "sort": "new", "limit": limit})

    def search_subreddit(self, subreddit, query, limit):
        return self.get(f"/r/{subreddit}/search.rss",
                        {"q": query, "sort": "new", "limit": limit,
                         "restrict_sr": 1})


# ---------------------------------------------------------------- main

def main(debug=False):
    cfg = load_config()
    state = load_state()
    seen_ids = set(state.get("seen_ids", []))

    watch_term = os.getenv("WATCH_TERM") or cfg.get("term") or DEFAULT_CONFIG["term"]
    watch_term = str(watch_term).strip()
    query = quote_if_needed(watch_term)

    user_agent = cfg.get("user_agent") or DEFAULT_CONFIG["user_agent"]
    limit = int(cfg.get("results_limit_per_query") or 50)

    if int(cfg.get("min_score") or 0) > 0:
        print("WARNING: min_score is set but RSS entries carry no score. "
              "This filter is being ignored.")

    print(f"Watch term: {watch_term!r} | query: {query!r} | "
          f"match_mode: {cfg.get('match_mode')} | "
          f"max_age_hours: {cfg.get('max_age_hours')}")

    fetcher = FeedFetcher(user_agent, debug=debug)

    matches = []
    requests_made = 0
    requests_ok = 0
    raw_total = 0

    if cfg.get("global_search"):
        requests_made += 1
        body = fetcher.search_sitewide(query, limit)
        if body is not None:
            requests_ok += 1
            posts = parse_feed(body)
            raw_total += len(posts)
            print(f"site-wide: {len(posts)} raw entries")
            matches.extend(filter_posts(posts, watch_term, cfg, debug))
    else:
        for sub in cfg.get("subreddits") or []:
            requests_made += 1
            body = fetcher.search_subreddit(sub, query, limit)
            if body is None:
                continue
            requests_ok += 1
            posts = parse_feed(body)
            raw_total += len(posts)
            print(f"r/{sub}: {len(posts)} raw entries")
            matches.extend(filter_posts(posts, watch_term, cfg, debug))
            time.sleep(2)

    print(f"Requests: {requests_ok}/{requests_made} succeeded | "
          f"raw entries: {raw_total} | passed filter: {len(matches)}")

    if requests_made and requests_ok == 0:
        print("FATAL: every feed request failed. Leaving matches.json untouched.")
        sys.exit(1)

    if raw_total == 0:
        print("NOTE: the feed came back empty. The requests succeeded, so the "
              "search term is probably too narrow.")
    elif not matches:
        print("NOTE: entries came back but all were filtered out. Raise "
              "max_age_hours, or set match_mode to \"off\".")

    new_matches = []
    for m in matches:
        mid = m.get("id")
        if not mid or mid in seen_ids:
            continue
        new_matches.append(m)
        seen_ids.add(mid)

    existing = load_json(MATCHES_PATH, {"matches": []})
    existing_matches = existing.get("matches", []) if isinstance(existing, dict) else []
    combined = (new_matches + existing_matches)[:MAX_KEEP]

    save_json(MATCHES_PATH, {
        "matches": combined,
        "generated_at": iso8601(now_ts()),
        "watch_term": watch_term,
    })

    state["seen_ids"] = list(seen_ids)[-1000:]
    state["last_run"] = iso8601(now_ts())
    state["watch_term"] = watch_term
    save_json(STATE_PATH, state)

    print(f"Wrote {len(new_matches)} new matches, total stored: {len(combined)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Reddit RSS watcher")
    parser.add_argument("--debug", action="store_true", help="verbose output")
    args = parser.parse_args()
    main(debug=args.debug)
