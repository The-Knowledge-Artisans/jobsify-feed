#!/usr/bin/env python3
"""
JobsifyAI XML job feed generator (Jooble / WhatJobs-style aggregator format).

Reads https://www.jobsify.ai/sitemap.xml, opens every /jobs/<slug>/ page,
pulls the schema.org JobPosting JSON-LD, and writes jobs.xml with one <job>
per OPEN, non-expired job.

Run daily (cron / Cloud Scheduler / Cloud Function) and serve the output at a
public URL such as https://www.jobsify.ai/feed/jobs.xml. Then submit that URL to
Jooble (ats@jooble.jobs), Adzuna ("Submit a feed") and Careerjet.

Usage:
    python3 jobsify_feed.py --out jobs.xml
    python3 jobsify_feed.py --out jobs.xml --limit 20      # quick test

Stdlib only. No salary field is emitted unless the JobPosting has baseSalary.
"""
import argparse
import html
import json
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone
from xml.sax.saxutils import escape

SITE = "https://www.jobsify.ai"
SITEMAP = SITE + "/sitemap.xml"
UA = "JobsifyAI-FeedBot/1.0 (+https://www.jobsify.ai)"
LD_RE = re.compile(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S | re.I)
JOB_URL_RE = re.compile(r"^https://www\.jobsify\.ai/jobs/[^/]+--[0-9a-f-]{36}/?$")


def fetch(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def sitemap_job_urls(xml_text):
    urls = re.findall(r"<loc>([^<]+)</loc>", xml_text)
    return [u.strip() for u in urls if "/jobs/" in u and JOB_URL_RE.match(u.strip())]


def extract_jobposting(page_html):
    """Return the first schema.org JobPosting dict found in the page, else None."""
    for block in LD_RE.findall(page_html):
        try:
            data = json.loads(html.unescape(block) if "&quot;" in block else block)
        except Exception:
            continue
        items = data if isinstance(data, list) else [data]
        for it in items:
            if isinstance(it, dict) and it.get("@type") == "JobPosting":
                return it
    return None


def parse_dt(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None


def cdata(text):
    return "<![CDATA[" + str(text).replace("]]>", "]]]]><![CDATA[>") + "]]>"


def desc_to_html(text):
    """JSON-LD description is plain text; the aggregators want HTML."""
    text = str(text or "").strip()
    if "<" in text and ">" in text and re.search(r"</?(p|br|ul|li|b|strong)\b", text, re.I):
        return text
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    return "".join("<p>" + html.escape(p).replace("\n", "<br/>") + "</p>" for p in paras)


def salary_text(jp):
    bs = jp.get("baseSalary")
    if not isinstance(bs, dict):
        return ""
    cur = bs.get("currency", "")
    val = bs.get("value", {})
    if isinstance(val, dict):
        lo, hi, unit = val.get("minValue"), val.get("maxValue"), val.get("unitText", "")
        if lo and hi:
            return f"{lo}-{hi} {cur} {unit}".strip()
        if val.get("value"):
            return f"{val['value']} {cur} {unit}".strip()
    return ""


def region_text(jp):
    loc = jp.get("jobLocation")
    if isinstance(loc, list) and loc:
        loc = loc[0]
    addr = (loc or {}).get("address", {}) if isinstance(loc, dict) else {}
    parts = [addr.get("addressLocality"), addr.get("addressRegion")]
    out = ", ".join(p for p in parts if p)
    return out or "India"


def build_feed(postings, now=None):
    """postings: list of (url, JobPosting dict). Returns XML string."""
    now = now or datetime.now(timezone.utc)
    out = ['<?xml version="1.0" encoding="UTF-8"?>', "<jobs>",
           f"  <publisher>{escape('JobsifyAI')}</publisher>",
           f"  <publisherurl>{SITE}</publisherurl>",
           f"  <lastBuildDate>{now.strftime('%Y-%m-%d %H:%M:%S')}</lastBuildDate>"]
    kept = skipped = 0
    for url, jp in postings:
        exp = parse_dt(jp.get("validThrough"))
        if exp and exp < now:
            skipped += 1
            continue
        jid = (jp.get("identifier") or {}).get("value") or url.rstrip("/").split("--")[-1]
        pub = parse_dt(jp.get("datePosted"))
        org = (jp.get("hiringOrganization") or {}).get("name", "")
        link = (jp.get("url") or url) + ("&" if "?" in (jp.get("url") or url) else "?") + \
            "utm_source=aggregator&utm_medium=xml_feed&utm_campaign=jobsify_feed"
        out += [f'  <job id="{escape(str(jid), {chr(34): "&quot;"})}">',
                f"    <link>{cdata(link)}</link>",
                f"    <name>{cdata(jp.get('title', ''))}</name>",
                f"    <region>{cdata(region_text(jp))}</region>",
                f"    <description>{cdata(desc_to_html(jp.get('description')))}</description>",
                f"    <pubdate>{(pub or now).strftime('%Y-%m-%d')}</pubdate>",
                f"    <updated>{(pub or now).strftime('%Y-%m-%d')}</updated>"]
        sal = salary_text(jp)
        if sal:
            out.append(f"    <salary>{cdata(sal)}</salary>")
        out.append(f"    <company>{cdata(org)}</company>")
        if jp.get("employmentType"):
            out.append(f"    <jobtype>{cdata(str(jp['employmentType']).replace('_', ' ').title())}</jobtype>")
        if exp:
            out.append(f"    <expire>{exp.strftime('%Y-%m-%d')}</expire>")
        out.append("  </job>")
        kept += 1
    out.append("</jobs>")
    sys.stderr.write(f"jobs written: {kept}, expired skipped: {skipped}\n")
    return "\n".join(out) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="jobs.xml")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--delay", type=float, default=0.3, help="seconds between page fetches")
    a = ap.parse_args()

    urls = sitemap_job_urls(fetch(SITEMAP))
    if a.limit:
        urls = urls[: a.limit]
    sys.stderr.write(f"{len(urls)} job pages in sitemap\n")
    postings = []
    for u in urls:
        try:
            jp = extract_jobposting(fetch(u))
        except Exception as e:
            sys.stderr.write(f"skip {u}: {e}\n")
            continue
        if jp:
            postings.append((u, jp))
        time.sleep(a.delay)
    with open(a.out, "w", encoding="utf-8") as f:
        f.write(build_feed(postings))


if __name__ == "__main__":
    main()
