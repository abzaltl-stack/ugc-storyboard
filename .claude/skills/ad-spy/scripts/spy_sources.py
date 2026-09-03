#!/usr/bin/env python3
"""
spy_sources — сбор АКТИВНЫХ рекламных креативов конкурентов из публичных
библиотек рекламы (Meta Ad Library, TikTok Creative Center, TikTok Ad Library).

Архитектура: мы не реверсим подписанные API-эндпоинты — они ломаются каждый
месяц. Вместо этого открываем публичную страницу библиотеки в Chromium через
Playwright и перехватываем её собственные XHR-ответы. Страница сама подписывает
свои запросы, мы только читаем прилетевший JSON.

Сырые ответы всегда падают в <out>/raw/ — если Meta переименовала поля и
нормализация вернула 0 объявлений, смотри реальный JSON в raw/ и правь
экстрактор, сбор переписывать не нужно.

Установка (один раз):
    pip install playwright
    python -m playwright install chromium
"""

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36")


# ─────────────────────────────── утилиты ───────────────────────────────


def _first(d, *keys, default=None):
    """Первое непустое значение из словаря по списку алиасов ключей."""
    if not isinstance(d, dict):
        return default
    for k in keys:
        if k in d and d[k] not in (None, "", [], {}):
            return d[k]
    return default


def _text(val):
    """Текст из поля, которое бывает строкой, {'text':...} или {'markup':{'__html':...}}."""
    if val is None:
        return None
    if isinstance(val, str):
        return re.sub(r"<[^>]+>", " ", val).strip() or None
    if isinstance(val, dict):
        for k in ("text", "markup", "__html", "value"):
            if k in val:
                return _text(val[k])
    if isinstance(val, list) and val:
        return _text(val[0])
    return None


def _iso(epoch):
    """Unix-время (сек или мс) → ISO-дата. None, если не разобрали."""
    if epoch in (None, "", 0):
        return None
    try:
        e = float(epoch)
    except (TypeError, ValueError):
        s = str(epoch)[:10]
        return s if re.match(r"^\d{4}-\d{2}-\d{2}$", s) else None
    if e > 1e11:  # миллисекунды
        e /= 1000.0
    if e < 1e8:   # не похоже на дату
        return None
    return datetime.fromtimestamp(e, tz=timezone.utc).date().isoformat()


def _days_active(start_iso, end_iso, is_active):
    if not start_iso:
        return None
    try:
        start = datetime.fromisoformat(start_iso).date()
    except ValueError:
        return None
    today = datetime.now(timezone.utc).date()
    end = today
    if end_iso and not is_active:
        try:
            end = datetime.fromisoformat(end_iso).date()
        except ValueError:
            end = today
    return max(0, (min(end, today) - start).days)


def _domain(url):
    m = re.search(r"https?://([^/?#]+)", url or "")
    return m.group(1).lower().replace("www.", "") if m else None


def walk_dicts(node):
    """Рекурсивный обход любого JSON: отдаёт все вложенные словари."""
    stack = [node]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            yield cur
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)


def parse_payload(body):
    """
    Ответы Meta/TikTok бывают: чистый JSON, NDJSON-стрим (несколько объектов
    подряд), JSON с префиксом `for (;;);`, иногда с мусором между объектами.
    Сканируем тело raw_decode-ом и возвращаем все разобранные объекты.
    """
    body = body.lstrip()
    if body.startswith("for (;;);"):
        body = body[len("for (;;);"):]
    out, dec, idx, n, misses = [], json.JSONDecoder(), 0, len(body), 0
    while idx < n and misses < 2000:
        while idx < n and body[idx] not in "[{":
            idx += 1
        if idx >= n:
            break
        try:
            obj, end = dec.raw_decode(body, idx)
        except json.JSONDecodeError:
            misses += 1
            idx += 1
            continue
        out.append(obj)
        idx = end
    return out


# ─────────────────────────── нормализация: Meta ───────────────────────────


def is_meta_ad(d):
    return isinstance(d, dict) and (
        "adArchiveID" in d or "ad_archive_id" in d
    ) and ("snapshot" in d or "collationCount" in d or "collation_count" in d)


def norm_meta(d, query=None):
    ad_id = str(_first(d, "adArchiveID", "ad_archive_id"))
    snap = _first(d, "snapshot", default={}) or {}
    if not isinstance(snap, dict):
        snap = {}

    videos, thumb = [], None
    for v in (_first(snap, "videos", default=[]) or []):
        if not isinstance(v, dict):
            continue
        url = _first(v, "video_hd_url", "videoHdUrl", "video_sd_url", "videoSdUrl")
        if url:
            videos.append(url)
        thumb = thumb or _first(v, "video_preview_image_url", "videoPreviewImageUrl")
    images = []
    for im in (_first(snap, "images", default=[]) or []):
        if isinstance(im, dict):
            u = _first(im, "original_image_url", "resized_image_url", "url")
            if u:
                images.append(u)
    thumb = thumb or (images[0] if images else None)

    start = _iso(_first(d, "startDate", "start_date", "startDateUnix"))
    end = _iso(_first(d, "endDate", "end_date"))
    active = bool(_first(d, "isActive", "is_active", default=False))
    link = _first(snap, "link_url", "linkUrl")

    return {
        "ad_id": f"meta-{ad_id}",
        "platform": "meta",
        "source_ref": ad_id,
        "advertiser": _first(d, "pageName", "page_name") or _first(snap, "page_name", "pageName"),
        "advertiser_id": str(_first(d, "pageID", "page_id") or "") or None,
        "ad_url": f"https://www.facebook.com/ads/library/?id={ad_id}",
        "is_active": active,
        "started_at": start,
        "ended_at": end,
        "days_active": _days_active(start, end, active),
        "variants": _first(d, "collationCount", "collation_count", default=1),
        "placements": _first(d, "publisherPlatform", "publisher_platform", default=[]),
        "media_type": "video" if videos else ("image" if images else None),
        "video_url": videos[0] if videos else None,
        "extra_videos": videos[1:],
        "thumb_url": thumb,
        "headline": _text(_first(snap, "title", "caption")),
        "body_text": _text(_first(snap, "body", "bodyText")),
        "cta_text": _first(snap, "cta_text", "ctaText"),
        "cta_type": _first(snap, "cta_type", "ctaType"),
        "link_url": link,
        "landing_domain": _domain(link) or _first(snap, "caption"),
        "display_format": _first(snap, "display_format", "displayFormat"),
        "reach": _first(d, "reachEstimate", "reach_estimate", "euTotalReach", "eu_total_reach"),
        "metrics": {},
        "source_query": query,
        "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


# ────────────────────────── нормализация: TikTok ──────────────────────────


def is_tiktok_ad(d):
    if not isinstance(d, dict):
        return False
    has_id = any(k in d for k in ("id", "ad_id", "material_id"))
    has_video = any(k in d for k in ("video_info", "videoInfo", "video_url"))
    return has_id and has_video


def norm_tiktok(d, query=None):
    ad_id = str(_first(d, "id", "ad_id", "material_id"))
    vi = _first(d, "video_info", "videoInfo", default={}) or {}
    if not isinstance(vi, dict):
        vi = {}
    video = _first(vi, "video_url", "videoUrl", "play_url") or _first(d, "video_url")
    if isinstance(video, dict):  # иногда {"720p": url, "480p": url}
        video = _first(video, "720p", "1080p", "480p", "360p") or next(iter(video.values()), None)
    metrics = {k: d[k] for k in ("ctr", "cvr", "cost", "like", "comment", "share", "play_six_rate",
                                 "impression", "click", "reach") if k in d}
    return {
        "ad_id": f"tt-{ad_id}",
        "platform": "tiktok",
        "source_ref": ad_id,
        "advertiser": _first(d, "brand_name", "brandName", "advertiser_name"),
        "advertiser_id": str(_first(d, "advertiser_id", default="") or "") or None,
        "ad_url": f"https://ads.tiktok.com/business/creativecenter/topads/{ad_id}/pc/en",
        "is_active": True,
        "started_at": _iso(_first(d, "first_seen", "start_time", "on_shelf_time", "create_time")),
        "ended_at": _iso(_first(d, "last_seen", "off_shelf_time", "end_time")),
        "days_active": None,
        "variants": 1,
        "placements": ["tiktok"],
        "media_type": "video",
        "video_url": video,
        "extra_videos": [],
        "thumb_url": _first(vi, "cover", "poster_url") or _first(d, "cover"),
        "headline": _first(d, "ad_title", "title", "name"),
        "body_text": _first(d, "ad_desc", "desc", "description"),
        "cta_text": _first(d, "cta", "call_to_action"),
        "cta_type": _first(d, "objective_key", "objective"),
        "link_url": _first(d, "landing_page", "click_url"),
        "landing_domain": _domain(_first(d, "landing_page", "click_url")),
        "display_format": _first(d, "industry_key", "industry"),
        "reach": None,
        "metrics": metrics,
        "duration": _first(vi, "duration"),
        "source_query": query,
        "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def is_ttlib_ad(d):
    return isinstance(d, dict) and "audit_status" not in d and (
        "ad_id" in d or "adId" in d) and ("videos" in d or "image_urls" in d or "video_url" in d)


def norm_ttlib(d, query=None):
    ad_id = str(_first(d, "ad_id", "adId"))
    vids = _first(d, "videos", default=[]) or []
    video = None
    if vids and isinstance(vids, list):
        v0 = vids[0]
        video = _first(v0, "video_url", "url") if isinstance(v0, dict) else v0
    video = video or _first(d, "video_url")
    start = _iso(_first(d, "first_shown_date", "first_shown", "start_time"))
    end = _iso(_first(d, "last_shown_date", "last_shown", "end_time"))
    return {
        "ad_id": f"ttlib-{ad_id}",
        "platform": "tiktok",
        "source_ref": ad_id,
        "advertiser": _first(d, "advertiser_name", "brand_name"),
        "advertiser_id": str(_first(d, "advertiser_id", default="") or "") or None,
        "ad_url": f"https://library.tiktok.com/ads/detail/?ad_id={ad_id}",
        "is_active": end is None,
        "started_at": start,
        "ended_at": end,
        "days_active": _days_active(start, end, end is None),
        "variants": 1,
        "placements": ["tiktok"],
        "media_type": "video" if video else "image",
        "video_url": video,
        "extra_videos": [],
        "thumb_url": _first(d, "cover", "image_urls"),
        "headline": _first(d, "ad_title", "title"),
        "body_text": _first(d, "ad_text", "text"),
        "cta_text": None,
        "cta_type": None,
        "link_url": _first(d, "landing_page"),
        "landing_domain": _domain(_first(d, "landing_page")),
        "display_format": None,
        "reach": _first(d, "unique_users_seen", "impression"),
        "metrics": {},
        "source_query": query,
        "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


# ──────────────────────────── описание источников ────────────────────────────


def meta_url(query, country="US", media="video", active_only=True, extra=""):
    if query.startswith("http"):
        return query
    base = "https://www.facebook.com/ads/library/?ad_type=all"
    base += f"&country={country}"
    base += "&active_status=active" if active_only else "&active_status=all"
    if re.fullmatch(r"\d{5,}", query.strip()):
        base += f"&view_all_page_id={query.strip()}&search_type=page"
    else:
        from urllib.parse import quote
        base += f"&q={quote(query)}&search_type=keyword_unordered"
    if media and media != "all":
        base += f"&media_type={media}"
    return base + extra


def tiktok_url(query, country="US", period=30, **_):
    if query.startswith("http"):
        return query
    from urllib.parse import quote
    url = ("https://ads.tiktok.com/business/creativecenter/inspiration/topads/pc/en"
           f"?period={period}&region={country}&adLanguage=en")
    if query and query != "*":
        url += f"&keyword={quote(query)}"
    return url


def ttlib_url(query, country="US", **_):
    if query.startswith("http"):
        return query
    from urllib.parse import quote
    return (f"https://library.tiktok.com/ads?region={country}&start_time=&end_time="
            f"&adv_name={quote(query)}&query_type=1")


SOURCES = {
    "meta": {
        "url": meta_url,
        "api": ("/api/graphql", "/ads/library/async"),
        "match": is_meta_ad,
        "norm": norm_meta,
        "id_key": lambda d: str(_first(d, "adArchiveID", "ad_archive_id")),
        "blocked": ("login", "checkpoint"),
    },
    "tiktok": {
        "url": tiktok_url,
        "api": ("creative_radar_api", "top_ads"),
        "match": is_tiktok_ad,
        "norm": norm_tiktok,
        "id_key": lambda d: str(_first(d, "id", "ad_id", "material_id")),
        "blocked": ("login",),
    },
    "tiktok-library": {
        "url": ttlib_url,
        "api": ("/api/v1/ads", "/api/ads"),
        "match": is_ttlib_ad,
        "norm": norm_ttlib,
        "id_key": lambda d: str(_first(d, "ad_id", "adId")),
        "blocked": ("login",),
    },
}


# ──────────────────────────────── сбор ────────────────────────────────


def harvest(source, query, out, limit=40, country="US", media="video",
            active_only=True, headful=False, state=None, scroll_pause=2.5,
            max_stagnant=4, period=30, verbose=True):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit("Нет playwright. Установи: pip install playwright && python -m playwright install chromium")

    spec = SOURCES[source]
    out = Path(out)
    raw_dir = out / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    start_url = spec["url"](query, country=country, media=media,
                            active_only=active_only, period=period)
    if verbose:
        print(f"[{source}] {start_url}")

    raw_hits, found, order = [], {}, []

    def consume(body, tag):
        payloads = parse_payload(body)
        if not payloads:
            return 0
        raw_hits.append(payloads)
        new = 0
        for p in payloads:
            for d in walk_dicts(p):
                if spec["match"](d):
                    key = spec["id_key"](d)
                    if key and key not in found:
                        found[key] = d
                        order.append(key)
                        new += 1
        if new and verbose:
            print(f"  +{new} (всего {len(found)})  ← {tag}")
        return new

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not headful)
        ctx_args = {"user_agent": UA, "viewport": {"width": 1440, "height": 900},
                    "locale": "en-US"}
        if state and Path(state).exists():
            ctx_args["storage_state"] = state
        ctx = browser.new_context(**ctx_args)
        page = ctx.new_page()

        def on_response(resp):
            try:
                if not any(a in resp.url for a in spec["api"]):
                    return
                if resp.status != 200:
                    return
                consume(resp.text(), resp.url.split("?")[0][-60:])
            except Exception:
                pass  # мёртвые/бинарные ответы не должны ронять сбор

        page.on("response", on_response)
        page.goto(start_url, wait_until="domcontentloaded", timeout=90_000)
        page.wait_for_timeout(5000)

        if any(b in page.url for b in spec["blocked"]):
            print(f"ВНИМАНИЕ: библиотека перекинула на {page.url} — нужен логин.\n"
                  f"Перезапусти с --headful --state {state or 'state.json'}, залогинься руками, "
                  f"сессия сохранится и следующий прогон пойдёт без окна.", file=sys.stderr)

        stagnant = 0
        while len(found) < limit and stagnant < max_stagnant:
            before = len(found)
            page.mouse.wheel(0, 6000)
            page.keyboard.press("End")
            page.wait_for_timeout(int(scroll_pause * 1000))
            stagnant = stagnant + 1 if len(found) == before else 0

        if state:
            ctx.storage_state(path=state)
        ctx.close()
        browser.close()

    ts = time.strftime("%Y%m%d-%H%M%S")
    for i, p in enumerate(raw_hits[:60]):
        (raw_dir / f"{source}-{ts}-{i:03d}.json").write_text(
            json.dumps(p, ensure_ascii=False)[:4_000_000], encoding="utf-8")

    records = []
    for key in order[:limit]:
        try:
            rec = spec["norm"](found[key], query=query)
            rec["_raw_keys"] = sorted(found[key].keys())[:40]
            records.append(rec)
        except Exception as e:  # одна кривая карточка не должна убивать прогон
            print(f"  пропущено {key}: {e}", file=sys.stderr)

    if not records and raw_hits:
        print(f"0 объявлений при {len(raw_hits)} перехваченных ответах — поля в API поменялись.\n"
              f"Смотри {raw_dir} и правь экстрактор в spy_sources.py.", file=sys.stderr)
    return records


def merge_into(path, records):
    """Дописывает новые объявления в ads.json, обновляя уже известные."""
    path = Path(path)
    existing = {}
    if path.exists():
        for r in json.loads(path.read_text(encoding="utf-8")):
            existing[r["ad_id"]] = r
    added, updated = 0, 0
    for r in records:
        r["last_seen_at"] = r["captured_at"]
        if r["ad_id"] in existing:
            prev = existing[r["ad_id"]]
            r["first_captured_at"] = prev.get("first_captured_at") or prev.get("captured_at")
            r["seen_runs"] = (prev.get("seen_runs") or 1) + 1
            # то, что уже наработано локально, переживает переcбор
            for k in ("local_video", "local_thumb", "creative_hash", "duplicate_of",
                      "dissected", "bytes", "notes"):
                if k in prev:
                    r[k] = prev[k]
            existing[r["ad_id"]] = r
            updated += 1
        else:
            r["first_captured_at"] = r["captured_at"]
            r["seen_runs"] = 1
            existing[r["ad_id"]] = r
            added += 1
    merged = sorted(existing.values(),
                    key=lambda x: (x.get("days_active") or 0, x.get("variants") or 0),
                    reverse=True)
    path.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    return added, updated, len(merged)


def main():
    ap = argparse.ArgumentParser(description="Сбор активных креативов конкурентов из библиотек рекламы")
    ap.add_argument("query", help="бренд, ключевое слово, page_id или готовый URL библиотеки")
    ap.add_argument("-s", "--source", default="meta", choices=list(SOURCES))
    ap.add_argument("-o", "--out", required=True, help="папка проекта (создаётся)")
    ap.add_argument("-n", "--limit", type=int, default=40)
    ap.add_argument("-c", "--country", default="US")
    ap.add_argument("--media", default="video", choices=["video", "image", "all"])
    ap.add_argument("--all-status", action="store_true", help="включая неактивные (по умолчанию только активные)")
    ap.add_argument("--period", type=int, default=30, help="TikTok Creative Center: окно в днях")
    ap.add_argument("--headful", action="store_true", help="показать браузер (нужно при логине/капче)")
    ap.add_argument("--state", help="файл сессии playwright (сохраняется и переиспользуется)")
    ap.add_argument("--scroll-pause", type=float, default=2.5)
    args = ap.parse_args()

    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    recs = harvest(args.source, args.query, out, limit=args.limit, country=args.country,
                   media=args.media, active_only=not args.all_status, headful=args.headful,
                   state=args.state, scroll_pause=args.scroll_pause, period=args.period)
    added, updated, total = merge_into(out / "ads.json", recs)
    print(f"\nСобрано {len(recs)} | новых {added} | обновлено {updated} | в базе {total}")
    print(f"→ {out/'ads.json'}")


if __name__ == "__main__":
    main()
