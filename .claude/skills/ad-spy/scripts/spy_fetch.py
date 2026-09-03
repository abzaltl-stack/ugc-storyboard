#!/usr/bin/env python3
"""
spy_fetch — качает креативы из ads.json на диск.

Фильтры важнее скорости: качать все подряд бессмысленно, интерес представляют
объявления, которые крутятся давно (значит, окупаются) и у которых много
вариантов (значит, на них льют бюджет). --min-days и --min-variants отсекают
шум до того, как потрачен трафик.

Одинаковый ролик часто лежит в библиотеке под десятком ad_id. Файлы
схлопываются по md5: дубликат не качается второй раз и не разбирается заново,
в записи проставляется duplicate_of.
"""

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36")
REFERER = {"meta": "https://www.facebook.com/", "tiktok": "https://ads.tiktok.com/"}


def download(url, dest, referer=None, tries=3):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "*/*",
        "Referer": referer or "https://www.google.com/",
    })
    last = None
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as f:
                shutil.copyfileobj(r, f)
            if dest.stat().st_size < 1024:
                raise IOError(f"файл {dest.stat().st_size} B — это не видео")
            return True, None
        except Exception as e:
            last = e
            dest.unlink(missing_ok=True)
            time.sleep(2 ** attempt)
    return False, str(last)


def ytdlp(url, dest):
    """Фолбэк для страниц (не прямых CDN-ссылок): TikTok, YouTube, Instagram."""
    if not shutil.which("yt-dlp"):
        return False, "нет yt-dlp"
    r = subprocess.run(["yt-dlp", "-q", "--no-warnings", "-f", "mp4/b",
                        "-o", str(dest), url], capture_output=True, text=True)
    if r.returncode == 0 and dest.exists():
        return True, None
    lines = [ln for ln in (r.stderr or "").strip().splitlines() if ln.strip()]
    return False, lines[-1][:200] if lines else "yt-dlp упал"


def md5(path, chunk=1 << 20):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(chunk), b""):
            h.update(b)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser(description="Скачивание креативов из ads.json")
    ap.add_argument("-o", "--out", required=True, help="папка проекта (та же, что у harvest)")
    ap.add_argument("--min-days", type=int, default=0, help="только объявления, живущие N+ дней")
    ap.add_argument("--min-variants", type=int, default=0)
    ap.add_argument("--active-only", action="store_true")
    ap.add_argument("-n", "--limit", type=int, default=0, help="0 = без ограничения")
    ap.add_argument("--thumbs", action="store_true", help="забрать ещё и превью")
    ap.add_argument("--sleep", type=float, default=1.0)
    ap.add_argument("--redownload", action="store_true")
    args = ap.parse_args()

    out = Path(args.out).expanduser()
    ads_path = out / "ads.json"
    if not ads_path.exists():
        sys.exit(f"нет {ads_path} — сначала harvest")
    ads = json.loads(ads_path.read_text(encoding="utf-8"))
    (out / "media").mkdir(parents=True, exist_ok=True)
    if args.thumbs:
        (out / "thumbs").mkdir(exist_ok=True)

    hashes = {a["creative_hash"]: a["ad_id"] for a in ads
              if a.get("creative_hash") and not a.get("duplicate_of")}

    queue = []
    for a in ads:
        if args.active_only and not a.get("is_active"):
            continue
        if (a.get("days_active") or 0) < args.min_days:
            continue
        if (a.get("variants") or 1) < args.min_variants:
            continue
        if a.get("local_video") and not args.redownload and Path(a["local_video"]).exists():
            continue
        if a.get("media_type") not in (None, "video"):
            continue
        queue.append(a)
    if args.limit:
        queue = queue[:args.limit]

    ok = fail = dup = 0
    errors = []
    for i, a in enumerate(queue, 1):
        dest = out / "media" / f"{a['ad_id']}.mp4"
        url = a.get("video_url")
        ref = REFERER.get(a.get("platform"))
        print(f"[{i}/{len(queue)}] {a['ad_id']} {a.get('advertiser') or ''}", flush=True)
        got, err = (download(url, dest, ref) if url else (False, "нет video_url"))
        if not got and a.get("ad_url"):
            got, err2 = ytdlp(a["ad_url"], dest)
            err = None if got else f"{err}; yt-dlp: {err2}"
        if not got:
            fail += 1
            errors.append((a["ad_id"], a.get("ad_url") or url, err))
            print(f"    ✗ {err}")
            continue

        h = md5(dest)
        a["creative_hash"] = h
        a["bytes"] = dest.stat().st_size
        if h in hashes and hashes[h] != a["ad_id"]:
            a["duplicate_of"] = hashes[h]
            dest.unlink()
            a["local_video"] = str(out / "media" / f"{hashes[h]}.mp4")
            dup += 1
            print(f"    = дубликат {hashes[h]}")
        else:
            hashes[h] = a["ad_id"]
            a.pop("duplicate_of", None)
            a["local_video"] = str(dest)
            ok += 1
        if args.thumbs and a.get("thumb_url"):
            download(a["thumb_url"], out / "thumbs" / f"{a['ad_id']}.jpg", ref, tries=1)
        time.sleep(args.sleep)

    ads_path.write_text(json.dumps(ads, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nСкачано {ok} | дубликатов {dup} | ошибок {fail} → {out/'media'}")
    for ad_id, url, err in errors:
        print(f"  ✗ {ad_id} {url} — {err}")


if __name__ == "__main__":
    main()
