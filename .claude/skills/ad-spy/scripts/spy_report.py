#!/usr/bin/env python3
"""
spy_report — сводит собранное в три артефакта:

  ads.csv             таблица объявлений, отсортированная по сроку жизни
  facts.md            борд: кто что крутит, что живёт дольше всех, что появилось
                      и что выключили с прошлого прогона, все крючки одной колонкой
  analysis_input.json компактный бандл (факты + транскрипты + супера + пути к
                      кадрам) — это то, что читает Claude, когда пишет разбор

Скрипт не делает выводов. Он расставляет статусы по сроку жизни и количеству
вариантов, потому что это единственные объективные сигналы «работает / не
работает», которые публичная библиотека вообще отдаёт.
"""

import argparse
import csv
import json
import statistics
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

CSV_COLS = ["ad_id", "platform", "advertiser", "status", "days_active", "variants",
            "is_active", "started_at", "ended_at", "duration", "aspect", "cta_text",
            "landing_domain", "headline", "ad_url", "local_video"]


def status_of(ad):
    d = ad.get("days_active")
    v = ad.get("variants") or 1
    if not ad.get("is_active"):
        return "выключено"
    if d is None:
        return "срок неизвестен"
    if d >= 60:
        return "эвергрин" + (" + масштаб" if v >= 5 else "")
    if d >= 21:
        return "проверено" + (" + масштаб" if v >= 5 else "")
    if d >= 7:
        return "в тесте"
    return "свежий тест"


def load(out):
    ads = json.loads((out / "ads.json").read_text(encoding="utf-8"))
    diss = {}
    ddir = out / "dissect"
    if ddir.exists():
        for f in ddir.glob("*.json"):
            try:
                diss[f.stem] = json.loads(f.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass
    return ads, diss


def latest_run(ads):
    seen = [a.get("last_seen_at") for a in ads if a.get("last_seen_at")]
    return max(seen) if seen else None


def rows(ads, diss):
    out = []
    for a in ads:
        d = diss.get(a["ad_id"]) or diss.get(a.get("duplicate_of") or "")
        r = {k: a.get(k) for k in CSV_COLS}
        r["status"] = status_of(a)
        if d:
            r["duration"] = d["tech"]["duration"]
            r["aspect"] = d["tech"]["aspect"]
        if r.get("headline"):
            r["headline"] = str(r["headline"]).replace("\n", " ")[:120]
        out.append(r)
    return sorted(out, key=lambda r: ((r["days_active"] or -1), r["variants"] or 0), reverse=True)


def md_table(headers, data):
    lines = ["| " + " | ".join(headers) + " |",
             "|" + "|".join(["---"] * len(headers)) + "|"]
    for row in data:
        cells = ["" if c is None else str(c).replace("|", "/").replace("\n", " ") for c in row]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def med(vals):
    vals = [v for v in vals if isinstance(v, (int, float))]
    return round(statistics.median(vals), 1) if vals else None


def build_facts(ads, diss, out, top=25):
    run = latest_run(ads)
    active = [a for a in ads if a.get("is_active")]
    # на первом прогоне «новое» — это вообще всё, такой раздел бесполезен
    has_history = any((a.get("seen_runs") or 1) > 1 for a in ads)
    fresh = ([a for a in ads if a.get("first_captured_at") == run and (a.get("seen_runs") or 1) == 1]
             if run and has_history else [])
    gone = [a for a in ads if run and a.get("last_seen_at") != run and (a.get("seen_runs") or 1) > 1]
    tbl = rows(ads, diss)

    L = [f"# Факты по конкурентам — {datetime.now(timezone.utc).date().isoformat()}", ""]
    L += [f"Объявлений в базе: **{len(ads)}**, активных: **{len(active)}**, "
          f"разобрано: **{len(diss)}**. Последний прогон: {run or '—'}.", ""]

    if fresh:
        L += ["## Появилось в этом прогоне", "",
              md_table(["ad", "бренд", "старт", "вариантов", "заголовок"],
                       [[a["ad_id"], a.get("advertiser"), a.get("started_at"), a.get("variants"),
                         (a.get("headline") or "")[:60]] for a in fresh[:20]]), ""]
    if gone:
        L += ["## Пропало из библиотеки с прошлого прогона (скорее всего выключено)", "",
              md_table(["ad", "бренд", "прожило дней", "последний раз виден"],
                       [[a["ad_id"], a.get("advertiser"), a.get("days_active"), a.get("last_seen_at")]
                        for a in gone[:20]]), ""]

    L += ["## Лидерборд по сроку жизни", "",
          "Срок жизни — единственный объективный сигнал окупаемости: убыточное объявление "
          "выключают за дни. Много вариантов при долгом сроке = на креатив льют бюджет.", "",
          md_table(["ad", "бренд", "дней", "вар.", "статус", "длит.", "формат", "CTA", "домен"],
                   [[r["ad_id"], r["advertiser"], r["days_active"], r["variants"], r["status"],
                     r["duration"], r["aspect"], r["cta_text"], r["landing_domain"]]
                    for r in tbl[:top]]), ""]

    by_adv = Counter(a.get("advertiser") or "—" for a in ads)
    L += ["## Кто сколько крутит", "",
          md_table(["бренд", "объявлений", "активных", "медиана дней"],
                   [[name,
                     n,
                     len([a for a in ads if (a.get("advertiser") or "—") == name and a.get("is_active")]),
                     med([a.get("days_active") for a in ads if (a.get("advertiser") or "—") == name])]
                    for name, n in by_adv.most_common(15)]), ""]

    if diss:
        durs = [d["tech"]["duration"] for d in diss.values()]
        rates = [d["edit"]["cut_rate_per_10s"] for d in diss.values()]
        speech = [d["hook_window"]["speech_starts_at"] for d in diss.values()]
        asp = Counter(d["tech"]["aspect"] for d in diss.values())
        L += ["## Продакшн-профиль разобранного", "",
              f"- медиана длительности: **{med(durs)} с** (разброс {min(durs)}–{max(durs)})",
              f"- медиана склеек на 10 с: **{med(rates)}**",
              f"- медиана старта речи: **{med(speech)} с**",
              f"- форматы кадра: {', '.join(f'{k} × {v}' for k, v in asp.most_common())}",
              f"- со звуковой дорожкой: {len([d for d in diss.values() if d['tech']['has_audio']])}/{len(diss)}",
              ""]

        hooks = []
        for d in sorted(diss.values(), key=lambda x: -(next(
                (a.get("days_active") or 0 for a in ads if a["ad_id"] == x["ad_id"]), 0))):
            hw = d["hook_window"]
            hooks.append([d["ad_id"], d.get("advertiser"),
                          (hw.get("first_words") or "—")[:80],
                          "; ".join(s["text"] for s in (hw.get("first_supers") or []))[:60] or "—",
                          hw.get("speech_starts_at"), hw.get("cuts_in_first_3s")])
        L += ["## Крючки: первые 3 секунды всех разобранных", "",
              "Речь — из транскрипта, супера — из OCR кадров. Пустой столбец речи при "
              "непустых суперах = ролик работает без звука.", "",
              md_table(["ad", "бренд", "первые слова", "супера ≤3 с", "речь с, с", "склеек ≤3 с"],
                       hooks[:top]), ""]

    (out / "facts.md").write_text("\n".join(L), encoding="utf-8")
    return len(fresh), len(gone)


def build_bundle(ads, diss, out, limit=0, max_transcript=2500):
    items = []
    for a in sorted(ads, key=lambda x: ((x.get("days_active") or -1), x.get("variants") or 0),
                    reverse=True):
        d = diss.get(a["ad_id"])
        if not d:
            continue
        tr = d.get("transcript") or []
        text = " ".join(f"[{s['t0']}] {s['text']}" for s in tr)[:max_transcript]
        items.append({
            "ad_id": a["ad_id"],
            "advertiser": a.get("advertiser"),
            "platform": a.get("platform"),
            "status": status_of(a),
            "days_active": a.get("days_active"),
            "variants": a.get("variants"),
            "started_at": a.get("started_at"),
            "ad_url": a.get("ad_url"),
            "headline": a.get("headline"),
            "body_text": (a.get("body_text") or "")[:600] or None,
            "cta_text": a.get("cta_text"),
            "landing_domain": a.get("landing_domain"),
            "placements": a.get("placements"),
            "metrics": a.get("metrics") or None,
            "tech": d["tech"],
            "edit": d["edit"],
            "hook_window": {k: v for k, v in d["hook_window"].items() if k != "frames"},
            "transcript_text": text or None,
            "transcript_engine": d.get("transcript_engine"),
            "wpm": d.get("wpm"),
            "supers": d.get("supers"),
            "contact_sheet": d.get("contact_sheet"),
            # ячейки контактного листа идут в этом же порядке — так читаются таймкоды
            "frames": [{"t": f["t"], "file": f["file"]} for f in d.get("frames", [])],
        })
        if limit and len(items) >= limit:
            break
    bundle = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "project": str(out), "ads_total": len(ads), "ads_dissected": len(items),
              "ads": items}
    (out / "analysis_input.json").write_text(
        json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
    return len(items)


def main():
    ap = argparse.ArgumentParser(description="Сводка по собранным креативам")
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--top", type=int, default=25, help="строк в таблицах facts.md")
    ap.add_argument("--bundle-limit", type=int, default=0, help="сколько объявлений класть в бандл")
    args = ap.parse_args()

    out = Path(args.out).expanduser()
    ads, diss = load(out)

    with open(out / "ads.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLS)
        w.writeheader()
        w.writerows(rows(ads, diss))

    fresh, gone = build_facts(ads, diss, out, top=args.top)
    n = build_bundle(ads, diss, out, limit=args.bundle_limit)
    print(f"ads.csv — {len(ads)} строк\n"
          f"facts.md — новых {fresh}, пропавших {gone}\n"
          f"analysis_input.json — {n} разобранных объявлений\n→ {out}")


if __name__ == "__main__":
    main()
