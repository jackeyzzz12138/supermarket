#!/usr/bin/env python3
"""Search the public arXiv Atom API using Python's standard library."""

import argparse
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
import fcntl
import hashlib
from http.client import HTTPException
import json
import math
import os
from pathlib import Path
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

ENDPOINT = "https://export.arxiv.org/api/query"
USER_AGENT = "Memoh-arXiv-Search/1.0 (https://github.com/felinics/supermarket)"
NS = {"a": "http://www.w3.org/2005/Atom",
      "o": "http://a9.com/-/spec/opensearch/1.1/",
      "x": "http://arxiv.org/schemas/atom"}
ID = re.compile(r"(?:\d{4}\.\d{4,5}|[a-z][a-z.-]+/\d{7})(?:v[1-9]\d*)?\Z")
MAX_BYTES = 8 * 1024 * 1024
CACHE_TTL = 86400
REQUEST_INTERVAL = 3.0


class ArxivError(Exception):
    """A user-facing API, transport or response error."""


def normalize_id(value):
    value = value.strip()
    if value.startswith(("https://", "http://")):
        parsed = urlsplit(value)
        if parsed.netloc not in {"arxiv.org", "www.arxiv.org", "export.arxiv.org"}:
            raise argparse.ArgumentTypeError("paper URLs must use arxiv.org")
        if not parsed.path.startswith(("/abs/", "/pdf/")):
            raise argparse.ArgumentTypeError("expected an arXiv /abs/ or /pdf/ URL")
        value = parsed.path[5:]
    if value.lower().startswith("arxiv:"):
        value = value[6:]
    value = value.removesuffix(".pdf")
    if not ID.fullmatch(value):
        raise argparse.ArgumentTypeError(f"invalid arXiv ID: {value}")
    return value


def parse_feed(data):
    # The API uses UTF-8. Reject DTDs/entities before ElementTree can expand them.
    try:
        xml = data.decode("utf-8-sig")
        if re.search(r"<!\s*(?:DOCTYPE|ENTITY)", xml, re.I):
            raise ArxivError("unexpected XML DTD or entity declaration")
        root = ET.fromstring(xml)
    except (UnicodeError, ET.ParseError) as exc:
        raise ArxivError("arXiv returned invalid UTF-8 Atom XML") from exc
    if root.tag != f"{{{NS['a']}}}feed":
        raise ArxivError("arXiv returned an unexpected response instead of an Atom feed")

    def text(node, field):
        return " ".join((node.findtext(field, default="", namespaces=NS)).split())

    papers = []
    for entry in root.findall("a:entry", NS):
        identifier = text(entry, "a:id")
        if identifier.startswith(("http://arxiv.org/api/errors", "https://arxiv.org/api/errors")):
            raise ArxivError(f"arXiv API error: {text(entry, 'a:summary')}")
        try:
            paper_id = normalize_id(identifier)
        except argparse.ArgumentTypeError as exc:
            raise ArxivError(f"arXiv returned an invalid paper ID: {identifier}") from exc
        primary = entry.find("x:primary_category", NS)
        papers.append({
            "id": paper_id,
            "title": text(entry, "a:title"),
            "authors": [text(author, "a:name") for author in entry.findall("a:author", NS)],
            "abstract": text(entry, "a:summary"),
            "published": text(entry, "a:published"),
            "updated": text(entry, "a:updated"),
            "categories": [cat.get("term", "") for cat in entry.findall("a:category", NS)],
            "primary_category": primary.get("term") if primary is not None else None,
            "url": f"https://arxiv.org/abs/{paper_id}",
            "pdf_url": f"https://arxiv.org/pdf/{paper_id}",
            "doi": text(entry, "x:doi") or None,
            "journal_ref": text(entry, "x:journal_ref") or None,
            "comment": text(entry, "x:comment") or None,
        })
    try:
        total = int(text(root, "o:totalResults"))
        start = int(text(root, "o:startIndex"))
    except ValueError as exc:
        raise ArxivError("arXiv response is missing valid pagination metadata") from exc
    return {"total_results": total, "start": start, "returned": len(papers), "papers": papers}


def retry_delay(header, attempt):
    if header:
        try:
            delay = float(header)
        except ValueError:
            try:
                delay = parsedate_to_datetime(header).timestamp() - time.time()
            except (ValueError, TypeError, OverflowError):
                delay = REQUEST_INTERVAL * (2 ** attempt)
    else:
        delay = REQUEST_INTERVAL * (2 ** attempt)
    if not math.isfinite(delay):
        raise ArxivError("arXiv returned an invalid Retry-After value; retry later")
    return max(REQUEST_INTERVAL, delay)


def fetch(params, cache_dir):
    url = ENDPOINT + "?" + urlencode(params)
    key = hashlib.sha256(url.encode()).hexdigest()
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached = cache_dir / f"{key}.json"
    # Hold the same lock during the network call and retries: only one connection.
    with (cache_dir / "requests.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            result = json.loads(cached.read_text())
            age = time.time() - cached.stat().st_mtime
            if 0 <= age < CACHE_TTL and isinstance(result.get("papers"), list):
                return {**result, "cache_hit": True}
        except (FileNotFoundError, ValueError, AttributeError):
            pass
        for attempt in range(3):
            lock.seek(0)
            try:
                next_request = float(lock.read() or "0")
                if not math.isfinite(next_request):
                    next_request = 0
            except ValueError:
                next_request = 0
            delay = max(0, next_request - time.time())
            if delay > 60:
                raise ArxivError("arXiv cooldown is still active; retry later")
            time.sleep(delay)
            lock.seek(0)
            lock.truncate()
            lock.write(str(time.time() + REQUEST_INTERVAL))
            lock.flush()
            try:
                request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/atom+xml"})
                with urlopen(request, timeout=30) as response:
                    data = response.read(MAX_BYTES + 1)
                if len(data) > MAX_BYTES:
                    raise ArxivError("arXiv response exceeded the 8 MiB limit; request fewer results")
                result = parse_feed(data)
            except HTTPError as exc:
                status, retry_after = exc.code, exc.headers.get("Retry-After")
                exc.close()
                if status == 429 or 500 <= status < 600:
                    delay = retry_delay(retry_after, attempt)
                    lock.seek(0)
                    lock.truncate()
                    lock.write(str(time.time() + delay))
                    lock.flush()
                    if delay > 60:
                        raise ArxivError("arXiv requested a longer cooldown; retry later") from exc
                    if attempt < 2:
                        continue
                raise ArxivError(f"arXiv HTTP {status}; retry later or check the query") from exc
            except (URLError, TimeoutError, HTTPException) as exc:
                raise ArxivError(f"arXiv network request failed: {exc}") from exc
            result.update({"request_url": url,
                           "fetched_at": datetime.now(timezone.utc).isoformat(),
                           "cache_hit": False})
            # Replace atomically so interruption cannot leave a partial cache entry.
            temporary = cached.with_suffix(".tmp")
            temporary.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
            temporary.replace(cached)
            return result


def calendar_date(value):
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError
        return parsed
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected a valid YYYY-MM-DD date") from exc


def build_params(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument("--query", help="arXiv query syntax, e.g. 'ti:attention AND cat:cs.CL'")
    selector.add_argument("--id", dest="ids", action="append", type=normalize_id, help="paper ID or URL; repeatable")
    parser.add_argument("--start", type=int, default=0, help="zero-based pagination offset")
    parser.add_argument("--max-results", type=int, default=10, help="page size, 1–100 (default: 10)")
    parser.add_argument("--sort-by", choices=["relevance", "submittedDate", "lastUpdatedDate"], default="relevance")
    parser.add_argument("--sort-order", choices=["ascending", "descending"], default="descending")
    parser.add_argument("--submitted-after", type=calendar_date, help="inclusive UTC date, YYYY-MM-DD")
    parser.add_argument("--submitted-before", type=calendar_date, help="inclusive UTC date, YYYY-MM-DD")
    args = parser.parse_args(argv)
    if args.start < 0 or not 1 <= args.max_results <= 100:
        parser.error("--start must be non-negative and --max-results must be between 1 and 100")
    if args.ids and (args.submitted_after or args.submitted_before):
        parser.error("submission date filters require --query")
    if args.ids and len(args.ids) > 100:
        parser.error("at most 100 IDs may be requested at once")
    if args.submitted_after and args.submitted_before and args.submitted_after > args.submitted_before:
        parser.error("--submitted-after cannot be later than --submitted-before")
    params = {"start": args.start, "max_results": args.max_results,
              "sortBy": args.sort_by, "sortOrder": args.sort_order}
    if args.ids:
        params["id_list"] = ",".join(dict.fromkeys(args.ids))
        params["max_results"] = len(set(args.ids))
    else:
        query = args.query.strip()
        if not query:
            parser.error("--query cannot be empty")
        if args.submitted_after or args.submitted_before:
            # arXiv rejects wildcard/remote-past date endpoints. Bound omitted
            # dates by arXiv's inception year and today (UTC), respectively.
            first_day = args.submitted_after or min(args.submitted_before, date(1991, 1, 1))
            last_day = args.submitted_before or max(args.submitted_after, datetime.now(timezone.utc).date())
            lower = first_day.isoformat().replace("-", "") + "0000"
            upper = last_day.isoformat().replace("-", "") + "2359"
            query = f"({query}) AND submittedDate:[{lower} TO {upper}]"
        params["search_query"] = query
    return params


def main(argv=None):
    params = build_params(argv)
    cache_root = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))
    cache_dir = Path(os.environ.get("ARXIV_CACHE_DIR", str(cache_root / "memoh-arxiv")))
    try:
        result = fetch(params, cache_dir)
    except (ArxivError, OSError) as exc:
        print(f"arxiv-search: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
