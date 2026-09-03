#!/usr/bin/env python3
"""
adspy — единая точка входа: собрать → скачать → разобрать → свести.

    python3 adspy.py selftest
    python3 adspy.py run "Nordace" -o ~/spy/nordace
    python3 adspy.py run "7412345678901" -o ~/spy/rival -s meta --min-days 21
    python3 adspy.py run "skincare" -o ~/spy/tt -s tiktok --country US --period 30

Повторный run по той же папке — это мониторинг: новые объявления допишутся,
пропавшие из библиотеки попадут в facts.md как выключенные.
Стадии запускаются и по отдельности (spy_sources / spy_fetch / spy_dissect /
spy_report) — так удобнее, когда падает только одна.
"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def sh(script, *args):
    cmd = [sys.executable, str(HERE / script), *[str(a) for a in args]]
    print(f"\n$ {' '.join(cmd[1:])}\n")
    return subprocess.run(cmd).returncode


def selftest():
    ok = True
    try:
        import playwright  # noqa: F401
        print("OK   playwright — сбор из библиотек")
    except ImportError:
        ok = False
        print("НЕТ  playwright — pip install playwright && python -m playwright install chromium")
    for b, need, why in (("ffmpeg", True, "разбор роликов"),
                         ("ffprobe", True, "техпараметры"),
                         ("yt-dlp", False, "фолбэк скачивания и профили конкурентов"),
                         ("tesseract", False, "OCR суперов")):
        found = shutil.which(b)
        print(("OK   " if found else ("НЕТ  " if need else "нет  ")) + f"{b} — {why}")
        ok = ok and (found or not need)
    try:
        import faster_whisper  # noqa: F401
        print("OK   faster-whisper — транскрипт")
    except ImportError:
        print("нет  faster-whisper — pip install faster-whisper (без него не будет скрипта)")
    print("\nГотов." if ok else "\nНе хватает обязательного — ставь по подсказкам выше.")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description="Разведка по видеорекламе конкурентов")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("selftest", help="проверить зависимости")

    r = sub.add_parser("run", help="полный проход: сбор → скачивание → разбор → сводка")
    r.add_argument("query", help="бренд, ключевое слово, page_id или URL библиотеки")
    r.add_argument("-o", "--out", required=True)
    r.add_argument("-s", "--source", default="meta", choices=["meta", "tiktok", "tiktok-library"])
    r.add_argument("-n", "--limit", type=int, default=40, help="сколько объявлений собрать")
    r.add_argument("-c", "--country", default="US")
    r.add_argument("--period", type=int, default=30)
    r.add_argument("--media", default="video", choices=["video", "image", "all"])
    r.add_argument("--all-status", action="store_true")
    r.add_argument("--headful", action="store_true")
    r.add_argument("--state")
    r.add_argument("--min-days", type=int, default=0, help="качать только то, что живёт N+ дней")
    r.add_argument("--min-variants", type=int, default=0)
    r.add_argument("--fetch-limit", type=int, default=0, help="потолок на скачивание")
    r.add_argument("--lang", help="язык речи для транскрипта")
    r.add_argument("--ocr-lang", default="eng")
    r.add_argument("--no-audio", action="store_true")
    r.add_argument("--skip-harvest", action="store_true", help="только скачать/разобрать имеющееся")

    a = ap.parse_args()
    if a.cmd == "selftest":
        sys.exit(selftest())

    out = Path(a.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)

    if not a.skip_harvest:
        args = [a.query, "-s", a.source, "-o", out, "-n", a.limit, "-c", a.country,
                "--media", a.media, "--period", a.period]
        if a.all_status:
            args.append("--all-status")
        if a.headful:
            args.append("--headful")
        if a.state:
            args += ["--state", a.state]
        if sh("spy_sources.py", *args) != 0:
            sys.exit("сбор упал — дальше идти нет смысла")

    fetch = ["-o", out, "--min-days", a.min_days, "--min-variants", a.min_variants, "--thumbs"]
    if a.fetch_limit:
        fetch += ["-n", a.fetch_limit]
    if not a.all_status:
        fetch.append("--active-only")
    sh("spy_fetch.py", *fetch)

    dis = ["-o", out, "--ocr-lang", a.ocr_lang]
    if a.lang:
        dis += ["--lang", a.lang]
    if a.no_audio:
        dis.append("--no-audio")
    sh("spy_dissect.py", *dis)

    sh("spy_report.py", "-o", out)
    print(f"\nГотово. Читай {out/'facts.md'}, разбор пиши по {out/'analysis_input.json'} "
          f"и контактным листам в {out/'frames'}.")


if __name__ == "__main__":
    main()
