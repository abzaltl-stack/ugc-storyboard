#!/usr/bin/env python3
"""Скачивание видео из TikTok по ссылке — без вотермарки, без зависимостей.

Примеры:
    python3 scripts/tiktok_download.py https://www.tiktok.com/@user/video/123456
    python3 scripts/tiktok_download.py -o downloads --json --cover <url1> <url2>
    python3 scripts/tiktok_download.py --list urls.txt
    python3 scripts/tiktok_download.py --yt-dlp <url>   # если разбор страницы сломался

Как это работает: скрипт открывает страницу поста, достаёт из неё JSON-состояние
(__UNIVERSAL_DATA_FOR_REHYDRATION__ или SIGI_STATE), берёт оттуда прямую ссылку на
mp4 и качает файл с теми же cookies и Referer — иначе CDN отдаёт 403.

Требуется Python 3.8+. TikTok периодически меняет разметку страницы; если прямой
путь перестал работать, используйте --yt-dlp (pip install yt-dlp).
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

PAGE_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Upgrade-Insecure-Requests": "1",
}

MEDIA_HEADERS = {
    "User-Agent": UA,
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
    "Referer": "https://www.tiktok.com/",
    "Origin": "https://www.tiktok.com",
    "Range": "bytes=0-",
    "Sec-Fetch-Dest": "video",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "cross-site",
}

UNIVERSAL_RE = re.compile(
    r'<script[^>]+id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>',
    re.DOTALL,
)
SIGI_RE = re.compile(
    r'<script[^>]+id="SIGI_STATE"[^>]*>(.*?)</script>',
    re.DOTALL,
)
VIDEO_ID_RE = re.compile(r"/(?:video|photo)/(\d+)")


class TikTokError(RuntimeError):
    """Ошибка, о которой имеет смысл рассказать пользователю без стектрейса."""


# ---------------------------------------------------------------- сеть


def build_opener() -> urllib.request.OpenerDirector:
    """Один opener на весь запуск: cookies с первого запроса нужны CDN."""
    jar = http.cookiejar.CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def open_url(
    opener: urllib.request.OpenerDirector,
    url: str,
    headers: dict,
    retries: int = 3,
    timeout: int = 30,
):
    """GET с повторами и экспоненциальной паузой. Возвращает открытый response."""
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            request = urllib.request.Request(url, headers=headers)
            return opener.open(request, timeout=timeout)
        except (urllib.error.URLError, urllib.error.HTTPError, OSError) as error:
            last_error = error
            if isinstance(error, urllib.error.HTTPError) and error.code in (403, 404):
                break
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
    raise TikTokError(f"не удалось открыть {url}: {last_error}")


def resolve_url(opener: urllib.request.OpenerDirector, url: str) -> str:
    """Разворачивает короткие ссылки vm./vt.tiktok.com и /t/ в полный URL поста."""
    url = url.strip()
    if not url.startswith("http"):
        url = "https://" + url
    parsed = urllib.parse.urlparse(url)
    if parsed.netloc.split(":")[0] not in {
        "vm.tiktok.com",
        "vt.tiktok.com",
        "m.tiktok.com",
        "www.tiktok.com",
        "tiktok.com",
    }:
        raise TikTokError(f"это не ссылка на TikTok: {url}")
    if not VIDEO_ID_RE.search(parsed.path):
        with open_url(opener, url, PAGE_HEADERS) as response:
            url = response.geturl()
    # query-параметры вида ?is_from_webapp=... только мешают
    parsed = urllib.parse.urlparse(url)
    return urllib.parse.urlunparse(parsed._replace(query="", fragment=""))


# ---------------------------------------------------------------- разбор страницы


def extract_state(html: str) -> dict:
    """Достаёт JSON-состояние страницы из inline-скрипта."""
    for pattern in (UNIVERSAL_RE, SIGI_RE):
        match = pattern.search(html)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                continue
    raise TikTokError(
        "на странице нет данных поста — TikTok мог показать капчу или страницу входа"
    )


def extract_item(state: dict, video_id: str | None) -> dict:
    """Находит itemStruct поста в любом из двух известных форматов состояния."""
    scope = state.get("__DEFAULT_SCOPE__", {})
    detail = scope.get("webapp.video-detail", {})
    if detail.get("statusCode") in (10216, 10222):
        raise TikTokError("пост приватный или доступен не всем")
    item = detail.get("itemInfo", {}).get("itemStruct")
    if item:
        return item

    # старый формат SIGI_STATE
    modules = state.get("ItemModule") or {}
    if modules:
        if video_id and video_id in modules:
            return modules[video_id]
        return next(iter(modules.values()))

    raise TikTokError("не нашёл данные видео в состоянии страницы")


def pick_video_urls(item: dict, prefer_watermark: bool = False) -> list[str]:
    """Собирает кандидатов на прямую ссылку, от лучшего качества к запасным."""
    video = item.get("video") or {}
    urls: list[str] = []

    if prefer_watermark and video.get("downloadAddr"):
        urls.append(video["downloadAddr"])

    bitrates = video.get("bitrateInfo") or []
    for entry in sorted(bitrates, key=lambda b: b.get("Bitrate", 0), reverse=True):
        urls.extend((entry.get("PlayAddr") or {}).get("UrlList") or [])

    for key in ("playAddr", "downloadAddr"):
        if video.get(key):
            urls.append(video[key])

    # сохраняем порядок, убираем дубли
    seen: set[str] = set()
    return [u for u in urls if u and not (u in seen or seen.add(u))]


def sanitize(name: str, limit: int = 60) -> str:
    """Делает из произвольной строки безопасное имя файла."""
    name = re.sub(r"[\\/:*?\"<>|\r\n\t]+", " ", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name[:limit].strip() or "tiktok"


# ---------------------------------------------------------------- скачивание


def download(
    opener: urllib.request.OpenerDirector,
    url: str,
    path: str,
    headers: dict,
    quiet: bool = False,
) -> int:
    """Качает файл потоком во временный .part, затем переименовывает."""
    tmp_path = path + ".part"
    downloaded = 0
    with open_url(opener, url, headers, timeout=60) as response:
        total = int(response.headers.get("Content-Length") or 0)
        with open(tmp_path, "wb") as handle:
            while True:
                chunk = response.read(1 << 16)
                if not chunk:
                    break
                handle.write(chunk)
                downloaded += len(chunk)
                if not quiet and total:
                    percent = downloaded * 100 // total
                    print(
                        f"\r  {percent:3d}%  {downloaded / 1048576:.1f} МБ",
                        end="",
                        file=sys.stderr,
                        flush=True,
                    )
    if not quiet:
        print(f"\r  готово  {downloaded / 1048576:.1f} МБ", file=sys.stderr)
    if downloaded == 0:
        os.remove(tmp_path)
        raise TikTokError("CDN вернул пустой ответ")
    os.replace(tmp_path, path)
    return downloaded


def download_via_yt_dlp(url: str, out_dir: str, quiet: bool) -> None:
    """Запасной путь: отдать ссылку yt-dlp, если он установлен."""
    binary = shutil.which("yt-dlp") or shutil.which("youtube-dl")
    if not binary:
        raise TikTokError("yt-dlp не установлен: pip install yt-dlp")
    command = [
        binary,
        "--no-warnings",
        "-o",
        os.path.join(out_dir, "%(uploader)s_%(id)s.%(ext)s"),
        url,
    ]
    if quiet:
        command.insert(1, "--quiet")
    result = subprocess.run(command)
    if result.returncode != 0:
        raise TikTokError(f"yt-dlp завершился с кодом {result.returncode}")


# ---------------------------------------------------------------- один пост


def process(url: str, args: argparse.Namespace) -> None:
    opener = build_opener()
    page_url = resolve_url(opener, url)

    if args.yt_dlp:
        download_via_yt_dlp(page_url, args.out, args.quiet)
        return

    id_match = VIDEO_ID_RE.search(urllib.parse.urlparse(page_url).path)
    video_id = id_match.group(1) if id_match else None

    with open_url(opener, page_url, PAGE_HEADERS) as response:
        html = response.read().decode("utf-8", "replace")

    item = extract_item(extract_state(html), video_id)
    video_id = item.get("id") or video_id or str(int(time.time()))
    author = (item.get("author") or {}).get("uniqueId") or "tiktok"
    base = f"{sanitize(author, 30)}_{video_id}"
    if args.title:
        caption = sanitize(item.get("desc") or "", 50)
        if caption:
            base = f"{base}_{caption}"
    prefix = os.path.join(args.out, base)

    image_post = item.get("imagePost") or {}
    if image_post.get("images"):
        save_slideshow(opener, image_post, prefix, args)
    else:
        save_video(opener, item, prefix, page_url, args)

    if args.cover:
        cover = (item.get("video") or {}).get("cover")
        if cover:
            download(opener, cover, prefix + "_cover.jpg", MEDIA_HEADERS, args.quiet)

    if args.music:
        music = (item.get("music") or {}).get("playUrl")
        if music:
            download(opener, music, prefix + "_music.mp3", MEDIA_HEADERS, args.quiet)

    if args.json:
        meta = {
            "id": video_id,
            "url": page_url,
            "author": author,
            "nickname": (item.get("author") or {}).get("nickname"),
            "desc": item.get("desc"),
            "createTime": item.get("createTime"),
            "duration": (item.get("video") or {}).get("duration"),
            "stats": item.get("stats"),
            "music": (item.get("music") or {}).get("title"),
        }
        with open(prefix + ".json", "w", encoding="utf-8") as handle:
            json.dump(meta, handle, ensure_ascii=False, indent=2)


def save_video(
    opener: urllib.request.OpenerDirector,
    item: dict,
    prefix: str,
    page_url: str,
    args: argparse.Namespace,
) -> None:
    path = prefix + ".mp4"
    if os.path.exists(path) and not args.overwrite:
        if not args.quiet:
            print(f"  пропускаю, файл уже есть: {path}", file=sys.stderr)
        return

    headers = dict(MEDIA_HEADERS, Referer=page_url)
    errors: list[str] = []
    for candidate in pick_video_urls(item, args.watermark):
        try:
            download(opener, candidate, path, headers, args.quiet)
            print(path)
            return
        except TikTokError as error:
            errors.append(str(error))
    raise TikTokError("ни одна прямая ссылка не отдала файл: " + "; ".join(errors[:3]))


def save_slideshow(
    opener: urllib.request.OpenerDirector,
    image_post: dict,
    prefix: str,
    args: argparse.Namespace,
) -> None:
    """Фото-посты отдаются набором картинок, а не видео."""
    for index, image in enumerate(image_post.get("images") or [], start=1):
        urls = ((image.get("imageURL") or {}).get("urlList")) or []
        if not urls:
            continue
        path = f"{prefix}_{index:02d}.jpg"
        if os.path.exists(path) and not args.overwrite:
            continue
        download(opener, urls[0], path, MEDIA_HEADERS, args.quiet)
        print(path)


# ---------------------------------------------------------------- CLI


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Скачивает видео из TikTok без вотермарки.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Скачивайте только то, на что у вас есть права: своё, или с разрешения автора.",
    )
    parser.add_argument("urls", nargs="*", help="ссылки на посты TikTok")
    parser.add_argument("--list", "-l", help="файл со ссылками, по одной на строку")
    parser.add_argument("--out", "-o", default="downloads", help="папка для файлов")
    parser.add_argument("--json", action="store_true", help="сохранять метаданные в .json")
    parser.add_argument("--cover", action="store_true", help="сохранять обложку")
    parser.add_argument("--music", action="store_true", help="сохранять аудиодорожку")
    parser.add_argument("--title", action="store_true", help="добавлять описание в имя файла")
    parser.add_argument("--watermark", action="store_true", help="версия с вотермаркой")
    parser.add_argument("--overwrite", action="store_true", help="перезаписывать существующие")
    parser.add_argument("--yt-dlp", action="store_true", help="качать через yt-dlp")
    parser.add_argument("--delay", type=float, default=1.0, help="пауза между ссылками, сек")
    parser.add_argument("--quiet", "-q", action="store_true", help="без прогресса")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)

    urls = list(args.urls)
    if args.list:
        with open(args.list, encoding="utf-8") as handle:
            urls += [
                line.strip()
                for line in handle
                if line.strip() and not line.startswith("#")
            ]
    if not urls:
        print("нужна хотя бы одна ссылка (или --list файл)", file=sys.stderr)
        return 2

    os.makedirs(args.out, exist_ok=True)

    failed = 0
    for index, url in enumerate(urls):
        if not args.quiet:
            print(f"[{index + 1}/{len(urls)}] {url}", file=sys.stderr)
        try:
            process(url, args)
        except TikTokError as error:
            failed += 1
            print(f"  ошибка: {error}", file=sys.stderr)
        except KeyboardInterrupt:
            print("\nпрервано", file=sys.stderr)
            return 130
        if index < len(urls) - 1 and args.delay:
            time.sleep(args.delay)

    if failed:
        print(f"не скачано: {failed} из {len(urls)}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
