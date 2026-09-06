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

Multiple terms: set "terms" in config.json to a list, e.g.

    "terms": ["bake croissants", "lamination", "cold proof"]

A post is a match if it matches ANY term, and each match records which one hit
in a "matched_term" field. The old single "term" key still works.

"query_mode" controls how they are fetched:
  "separate" (default) - one feed request per term. Each term gets its own full
                         results_limit, so a busy term can't crowd out a quiet
                         one. Costs one request per term.
  "combined"           - one request using Reddit's OR syntax,
                         q=("bake croissants" OR lamination). Cheaper, but all
                         terms share a single results_limit.

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
import math
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
    "terms": ["bake croissants"],
    "term": None,             # legacy single-term key, still honoured
    "global_search": True,
    "subreddits": ["baking", "AskCulinary", "breadit"],
    "min_score": 0,           # ignored: RSS carries no score
    "max_age_hours": 168,
    "results_limit_per_query": 50,
    "terms_per_run": 8,           # 0 = every term every run
    "request_delay_seconds": 5,
    "match_mode": "loose",    # loose | strict | off
    "query_mode": "separate",  # separate | combined
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


def resolve_terms(cfg):
    """Build the ordered, de-duplicated list of watch terms.

    Priority: WATCH_TERM env var -> config "terms" list -> config "term" string.
    The env var and the legacy "term" key both accept a comma-separated list, so
    you can pass several terms to a manual workflow_dispatch run.
    """
    raw = os.getenv("WATCH_TERM")
    if raw:
        candidates = raw.split(",")
    else:
        candidates = cfg.get("terms") or cfg.get("term") or DEFAULT_CONFIG["terms"]
        if isinstance(candidates, str):
            candidates = candidates.split(",")

    terms, seen = [], set()
    for t in candidates:
        t = str(t).strip()
        if t and t.lower() not in seen:
            seen.add(t.lower())
            terms.append(t)
    return terms


def build_combined_query(terms):
    """q=("bake croissants" OR lamination) - one request covering every term."""
    if len(terms) == 1:
        return quote_if_needed(terms[0])
    return "(" + " OR ".join(quote_if_needed(t) for t in terms) + ")"


def norm_terms(terms):
    return {str(t).strip().lower() for t in terms if str(t).strip()}


def reconcile_with_previous_terms(existing_matches, prev_terms, terms, debug=False):
    """Drop stored matches that belong to terms we are no longer watching.

    `terms` here must be the FULL configured term list, never the per-run batch.
    Comparing against a batch would delete matches for every term that simply
    was not scheduled this run.

    Returns (retained_matches, seen_ids_or_None, changed, added_terms).

    When the term list changes, seen_ids is rebuilt from the retained matches
    only. That matters: seen_ids is what stops a post being added twice, so if
    we kept the old set, a post originally found under a retired term would be
    permanently suppressed even if it matches a term you add later.

    Matches saved before matched_term existed have no term to check, so they
    are treated as belonging to the old configuration and dropped.
    """
    current = norm_terms(terms)
    previous = norm_terms(prev_terms or [])

    if not previous:
        # First run, or state written by an older version. Nothing to compare.
        return existing_matches, None, False, set()

    if previous == current:
        return existing_matches, None, False, set()

    retained, dropped = [], []
    for m in existing_matches:
        mt = str(m.get("matched_term") or "").strip().lower()
        if mt and mt in current:
            retained.append(m)
        else:
            dropped.append(m)

    added = sorted(current - previous)
    removed = sorted(previous - current)
    print("Watch terms changed since the last run.")
    if added:
        print(f"  added:   {added}")
    if removed:
        print(f"  removed: {removed}")
    print(f"  dropped {len(dropped)} stored match(es) from retired terms, "
          f"kept {len(retained)}")

    if debug and dropped:
        for m in dropped[:10]:
            print(f"    [debug] dropped {m.get('id')} "
                  f"(matched_term={m.get('matched_term')!r}): {m.get('title')}")

    return retained, {m["id"] for m in retained if m.get("id")}, True, current - previous


def plan_batch(all_terms, state, per_run, terms_changed, added, debug=False):
    """Pick which terms to search this run, round-robin across runs.

    Reddit's public .rss endpoints are undocumented but measured at roughly
    15-18 sequential requests from one IP before everything starts returning
    429, so a long term list has to be spread over several runs rather than
    fired off at once.

    The pending queue lives in state.json. Each run takes the first
    `per_run` terms; when the queue empties it refills from the full list, so
    every term gets its turn. Terms removed from config drop out of the queue,
    and when the term list changes the cycle restarts with newly added terms
    first so a term you just added is searched immediately.

    Returns (batch, remaining_queue).
    """
    if per_run <= 0 or per_run >= len(all_terms):
        return list(all_terms), []

    current = {t.lower() for t in all_terms}
    queue = [t for t in (state.get("term_queue") or []) if t.lower() in current]

    if terms_changed:
        head = [t for t in all_terms if t.lower() in added]
        tail = [t for t in all_terms if t.lower() not in added]
        queue = head + tail
        if debug and head:
            print(f"[debug] term list changed - restarting cycle, new terms first: {head}")

    if not queue:
        queue = list(all_terms)
        if debug:
            print("[debug] term queue empty - starting a fresh cycle")

    return queue[:per_run], queue[per_run:]


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


def first_matching_term(text, terms, mode):
    """Return the first term that matches, or None. Any term is enough."""
    for t in terms:
        if term_matches(text, t, mode):
            return t
    return None


def filter_posts(posts, terms, cfg, debug=False):
    """Keep posts that are recent enough and match at least one term."""
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
        hit = first_matching_term(haystack, terms, mode)
        if hit is None:
            dropped["term"] += 1
            continue
        p["matched_term"] = hit
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

    terms = resolve_terms(cfg)
    if not terms:
        print("FATAL: no watch terms configured. Set \"terms\" in config.json.")
        sys.exit(1)

    # Load what we already published, and reconcile it against the term list
    # BEFORE fetching, so seen_ids is correct when we dedupe below.
    existing = load_json(MATCHES_PATH, {"matches": []})
    existing_matches = existing.get("matches", []) if isinstance(existing, dict) else []
    prev_terms = state.get("watch_terms")
    if prev_terms is None and state.get("watch_term"):
        prev_terms = [t.strip() for t in str(state["watch_term"]).split(",")]

    existing_matches, rebuilt_seen, terms_changed, added = reconcile_with_previous_terms(
        existing_matches, prev_terms, terms, debug)
    if rebuilt_seen is not None:
        seen_ids = rebuilt_seen

    user_agent = cfg.get("user_agent") or DEFAULT_CONFIG["user_agent"]
    limit = int(cfg.get("results_limit_per_query") or 50)
    query_mode = (cfg.get("query_mode") or "separate").lower()
    subs = cfg.get("subreddits") or []
    per_run = int(cfg.get("terms_per_run") if cfg.get("terms_per_run") is not None else 8)
    delay = float(cfg.get("request_delay_seconds") or 5)

    # `terms` is everything configured; `batch` is what this run actually searches.
    batch, queue_remaining = plan_batch(terms, state, per_run, terms_changed, added, debug)

    if int(cfg.get("min_score") or 0) > 0:
        print("WARNING: min_score is set but RSS entries carry no score. "
              "This filter is being ignored.")

    print(f"Configured terms: {len(terms)}")
    if len(batch) < len(terms):
        runs_per_cycle = math.ceil(len(terms) / max(per_run, 1))
        print(f"This run searches {len(batch)} of them: {batch}")
        print(f"  {len(queue_remaining)} queued for later runs "
              f"({runs_per_cycle} runs per full cycle)")
    else:
        print(f"This run searches all of them: {batch}")
    print(f"query_mode: {query_mode} | match_mode: {cfg.get('match_mode')} | "
          f"max_age_hours: {cfg.get('max_age_hours')} | delay: {delay}s")

    # Work out the list of (query, terms_to_match_against) pairs up front.
    if query_mode == "combined":
        query_plan = [(build_combined_query(batch), batch)]
    else:
        query_plan = [(quote_if_needed(t), [t]) for t in batch]

    targets = ["sitewide"] if cfg.get("global_search") else list(subs)
    total_requests = len(query_plan) * len(targets)
    if total_requests > 12:
        print(f"WARNING: this run will make {total_requests} feed requests "
              f"({len(query_plan)} queries x {len(targets)} targets). Reddit's "
              f".rss endpoints start returning 429 somewhere around 15-18 "
              f"sequential requests per IP. Lower terms_per_run, or switch "
              f"query_mode to \"combined\".")

    fetcher = FeedFetcher(user_agent, debug=debug)

    matches = []
    requests_made = 0
    requests_ok = 0
    raw_total = 0
    per_term_raw = {t: 0 for t in batch}

    for query, match_terms in query_plan:
        for target in targets:
            requests_made += 1
            if target == "sitewide":
                body = fetcher.search_sitewide(query, limit)
                label = f"site-wide {query!r}"
            else:
                body = fetcher.search_subreddit(target, query, limit)
                label = f"r/{target} {query!r}"

            if body is None:
                continue

            requests_ok += 1
            posts = parse_feed(body)
            raw_total += len(posts)
            for t in match_terms:
                per_term_raw[t] = per_term_raw.get(t, 0) + len(posts)
            kept = filter_posts(posts, match_terms, cfg, debug)
            print(f"{label}: {len(posts)} raw entries, {len(kept)} kept")
            matches.extend(kept)

            # Pace requests. Steady spacing survives better than bursts.
            if requests_made < total_requests:
                time.sleep(delay)

    print(f"Requests: {requests_ok}/{requests_made} succeeded | "
          f"raw entries: {raw_total} | passed filter: {len(matches)}")

    if requests_made and requests_ok == 0:
        print("FATAL: every feed request failed. Leaving matches.json untouched.")
        sys.exit(1)

    # Flag terms that returned nothing, so a dud term is obvious in the log.
    quiet = [t for t in batch if per_term_raw.get(t, 0) == 0]
    if quiet and requests_ok:
        print(f"NOTE: these terms returned no entries at all: {quiet}")

    if raw_total and not matches:
        print("NOTE: entries came back but all were filtered out. Raise "
              "max_age_hours, or set match_mode to \"off\".")

    # Dedupe within this run (a post can match several terms), then against
    # everything we have already published.
    new_matches = []
    batch_ids = set()
    for m in matches:
        mid = m.get("id")
        if not mid or mid in batch_ids or mid in seen_ids:
            continue
        batch_ids.add(mid)
        new_matches.append(m)
        seen_ids.add(mid)

    if debug and new_matches:
        by_term = {}
        for m in new_matches:
            by_term[m["matched_term"]] = by_term.get(m["matched_term"], 0) + 1
        print(f"[debug] new matches by term: {by_term}")

    combined = (new_matches + existing_matches)[:MAX_KEEP]

    save_json(MATCHES_PATH, {
        "matches": combined,
        "generated_at": iso8601(now_ts()),
        # Every configured term, so the page shows what is being watched
        # overall rather than just this run's slice.
        "watch_terms": terms,
        "searched_this_run": batch,
        # Kept so anything reading the old single-term field still works.
        "watch_term": ", ".join(terms),
    })

    state["seen_ids"] = list(seen_ids)[-1000:]
    state["last_run"] = iso8601(now_ts())
    state["watch_terms"] = terms
    state["watch_term"] = ", ".join(terms)
    # Advance the round-robin. Only reached on a successful run, so a failed
    # run retries the same batch instead of skipping those terms.
    state["term_queue"] = queue_remaining
    save_json(STATE_PATH, state)

    print(f"Wrote {len(new_matches)} new matches, total stored: {len(combined)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Reddit RSS watcher")
    parser.add_argument("--debug", action="store_true", help="verbose output")
    args = parser.parse_args()
    main(debug=args.debug)
