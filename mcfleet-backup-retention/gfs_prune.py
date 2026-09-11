#!/usr/bin/env python3
# stdin = filenames <prefix>-YYYYMMDD_HHMMSSZ.tar.gz ; prints ones to DELETE.
# KEEP: newest-per-day (last 7 days) + newest-per-7d-bucket (last 3 = ~2wk) + newest-per-month (2 recent).
# argv[1] = filename prefix to match (default world-backup, kept for back-compat with existing callers/crontabs).
import sys, re, datetime
prefix = sys.argv[1] if len(sys.argv) > 1 else 'world-backup'
now=datetime.datetime.utcnow()
items=[]
for l in sys.stdin:
    f=l.strip(); m=re.search(re.escape(prefix) + r'-(\d{8})_(\d{6})Z', f)
    if m: items.append((datetime.datetime.strptime(m.group(1)+m.group(2),'%Y%m%d%H%M%S'), f))
items.sort(key=lambda x:x[0], reverse=True)
keep=set()
seen=set()
for t,f in items:                       # daily: newest per day, last 7 days
    if (now-t).days<7 and t.strftime('%Y%m%d') not in seen: seen.add(t.strftime('%Y%m%d')); keep.add(f)
seen=set()
for t,f in items:                       # weekly: newest per 7d bucket, buckets 0-2 (~2 weeks)
    b=(now-t).days//7
    if b<3 and b not in seen: seen.add(b); keep.add(f)
seen=[]
for t,f in items:                       # monthly: newest per month, 2 recent months
    ym=(t.year,t.month)
    if ym not in seen and len(seen)<2: seen.append(ym); keep.add(f)
for t,f in items:
    if f not in keep: print(f)
