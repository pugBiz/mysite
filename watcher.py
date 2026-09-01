#!/usr/bin/env python3
"""
watcher.py
- Reads config.json (repo root) and optional WATCH_TERM env var.
- Searches Reddit (site-wide or per-subreddit) using public JSON endpoints.
- Filters results by score and age, deduplicates using state.json.
- Writes matches.json and updates state.json.

Notes:
- For higher rate limits and reliability, you can provide Reddit OAuth credentials
  as secrets and set REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET in the workflow.
  This script currently uses the public JSON endpoints and a polite User-Agent.
"""

import os
import json
import time
import requests
from datetime import datetime, timezone, timedelta

ROOT = os.getcwd()
CONFIG_PATH = os.path.join(ROOT, "config.json")
STATE_PATH = os.path.join(ROOT, "state.json")
MATCHES_PATH = os.path.join(ROOT, "matches.json")

# Defaults
DEFAULT_CONFIG = {
    "term": "bake croissants",
    "global_search": True,
    "subreddits": ["baking", "AskCulinary", "breadit"],
    "min_score": 1,
    "max_age_hours": 48,
    "results_limit_per_query": 25,
    "user_agent": "CroissantWatcher/1.0 (by github.com/your_github_username)"
}

def load_json(path, default=None):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default
    return default

def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def load_config():
    cfg = load_json(CONFIG_PATH, {})
    merged = DEFAULT_CONFIG.copy()
    merged.update(cfg or {})
    return merged

def load_state():
    s = load_json(STATE_PATH, {})
    if not isinstance(s, dict):
        s = {}
    # state will store last_seen_ids set
    s.setdefault("seen_ids", [])
    return s

def now_ts():
    return int(time.time())

def iso8601(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()

def reddit_search_sitewide(query, limit, user_agent, after=None):
    # Uses Reddit's public search endpoint (no OAuth). Rate-limited; be polite.
    url = "https://www.reddit.com/search.json"
    params = {"q": query, "sort": "new", "limit": limit}
    if after:
        params["after"] = after
    headers = {"User-Agent": user_agent}
    r = requests.get(url, params=params, headers=headers, timeout=20)
    r.raise_for_status()
    return r.json()

def reddit_search_subreddit(subreddit, query, limit, user_agent):
    url = f"https://www.reddit.com/r/{subreddit}/search.json"
    params = {"q": query, "sort": "new", "limit": limit, "restrict_sr": 1}
    headers = {"User-Agent": user_agent}
    r = requests.get(url, params=params, headers=headers, timeout=20)
    r.raise_for_status()
    return r.json()

def extract_posts_from_listing(listing_json):
    posts = []
    children = listing_json.get("data", {}).get("children", [])
    for c in children:
        d = c.get("data", {})
        posts.append({
            "id": d.get("id"),
            "fullname": d.get("name"),  # t3_<id>
            "title": d.get("title"),
            "url": "https://www.reddit.com" + d.get("permalink", "") if d.get("permalink") else d.get("url"),
            "excerpt": d.get("selftext") or "",
            "subreddit": d.get("subreddit"),
            "author": d.get("author"),
            "score": d.get("score", 0),
            "created_utc": d.get("created_utc"),
            "type": "post"
        })
    return posts

def filter_posts(posts, term_lower, min_score, max_age_hours):
    out = []
    cutoff_ts = now_ts() - int(max_age_hours * 3600)
    for p in posts:
        if not p.get("id"):
            continue
        if p.get("score", 0) < min_score:
            continue
        created = int(p.get("created_utc") or 0)
        if created and created < cutoff_ts:
            continue
        # match term in title or excerpt or subreddit
        title = (p.get("title") or "").lower()
        excerpt = (p.get("excerpt") or "").lower()
        subreddit = (p.get("subreddit") or "").lower()
        if term_lower in title or term_lower in excerpt or term_lower in subreddit:
            p["found_at"] = iso8601(now_ts())
            out.append(p)
    return out

def main():
    cfg = load_config()
    state = load_state()
    seen_ids = set(state.get("seen_ids", []))

    # Priority: env WATCH_TERM -> config term -> default
    watch_term = os.getenv("WATCH_TERM") or cfg.get("term") or DEFAULT_CONFIG["term"]
    term_lower = watch_term.lower()

    user_agent = cfg.get("user_agent") or DEFAULT_CONFIG["user_agent"]
    limit = int(cfg.get("results_limit_per_query", 25))
    min_score = int(cfg.get("min_score", 1))
    max_age_hours = int(cfg.get("max_age_hours", 48))

    matches = []

    try:
        if cfg.get("global_search"):
            # site-wide search
            listing = reddit_search_sitewide(watch_term, limit, user_agent)
            posts = extract_posts_from_listing(listing)
            filtered = filter_posts(posts, term_lower, min_score, max_age_hours)
            matches.extend(filtered)
        else:
            # per-subreddit search
            subs = cfg.get("subreddits") or []
            for sub in subs:
                try:
                    listing = reddit_search_subreddit(sub, watch_term, limit, user_agent)
                    posts = extract_posts_from_listing(listing)
                    filtered = filter_posts(posts, term_lower, min_score, max_age_hours)
                    matches.extend(filtered)
                except requests.HTTPError as e:
                    # skip sub if it doesn't exist or request fails
                    print(f"Warning: subreddit {sub} search failed: {e}")
    except Exception as e:
        print("Error during Reddit search:", e)

    # Deduplicate by id and exclude already seen
    new_matches = []
    for m in matches:
        mid = m.get("id")
        if not mid:
            continue
        if mid in seen_ids:
            continue
        new_matches.append(m)
        seen_ids.add(mid)

    # Sort newest first by created_utc if available
    new_matches.sort(key=lambda x: x.get("created_utc") or 0, reverse=True)

    # Load existing matches.json (so we can include previous matches if desired)
    existing = load_json(MATCHES_PATH, {"matches": []})
    existing_matches = existing.get("matches", []) if isinstance(existing, dict) else []

    # Prepend new matches to existing list (keep recent history)
    combined = new_matches + existing_matches
    # Optionally trim to a reasonable size (e.g., 200)
    MAX_KEEP = 200
    combined = combined[:MAX_KEEP]

    out = {"matches": combined, "generated_at": iso8601(now_ts()), "watch_term": watch_term}

    save_json(MATCHES_PATH, out)

    # Update state
    state["seen_ids"] = list(seen_ids)[-1000:]  # keep a rolling set
    state["last_run"] = iso8601(now_ts())
    state["watch_term"] = watch_term
    save_json(STATE_PATH, state)

    print(f"Wrote {len(new_matches)} new matches, total stored: {len(combined)}")

if __name__ == "__main__":
    main()
