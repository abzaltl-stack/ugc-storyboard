#!/usr/bin/env python3
"""
spy_dissect — разбор скачанного креатива на измеримые части.

Что достаём из каждого ролика:
  • техника        — длительность, формат кадра, fps, есть ли звук
  • монтаж         — таймкоды склеек, средняя длина плана, склейки в первые 3 с
  • кадры          — плотная нарезка hook-окна (0–3 с) + по кадру на каждый план,
                     плюс контактный лист одной картинкой для быстрого просмотра
  • звук           — где начинается речь, доля тишины
  • скрипт         — транскрипт с таймкодами (faster-whisper / whisper / whisper.cpp)
  • супера         — OCR экранного текста по кадрам (tesseract, если стоит)

Скрипт считает только факты. Выводы (крючок, структура, углы) делает Claude
по этим фактам и картинкам — см. references/analysis-framework.md.

Зависимости: ffmpeg/ffprobe обязательны. Остальное опционально:
    pip install faster-whisper          # транскрипт
    brew install tesseract              # OCR суперов (apt install tesseract-ocr)
"""

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

SCENE_THRESHOLD = 0.25
HOOK_TIMES = [0.2, 0.8, 1.5, 2.2, 3.0]   # плотная выборка по hook-окну
MAX_FRAMES = 18
SHEET_COLS = 4


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def have(binname):
    return shutil.which(binname) is not None


# ─────────────────────────────── техника ───────────────────────────────


def probe(video):
    r = run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format",
             "-show_streams", str(video)])
    if r.returncode != 0:
        raise RuntimeError(f"ffprobe: {r.stderr.strip()[:200]}")
    data = json.loads(r.stdout)
    v = next((s for s in data["streams"] if s["codec_type"] == "video"), {})
    a = next((s for s in data["streams"] if s["codec_type"] == "audio"), None)
    num, den = (v.get("r_frame_rate") or "0/1").split("/")
    fps = round(float(num) / float(den), 2) if float(den or 0) else None
    w, h = v.get("width"), v.get("height")
    return {
        "duration": round(float(data["format"].get("duration") or 0), 2),
        "width": w, "height": h,
        "aspect": aspect_name(w, h),
        "fps": fps,
        "has_audio": a is not None,
        "vcodec": v.get("codec_name"),
        "size_bytes": int(data["format"].get("size") or 0),
    }


def aspect_name(w, h):
    if not w or not h:
        return None
    r = w / h
    for name, val in (("9:16", 0.5625), ("4:5", 0.8), ("1:1", 1.0), ("16:9", 1.777)):
        if abs(r - val) < 0.06:
            return name
    return f"{round(r, 2)}:1"


# ─────────────────────────────── монтаж ───────────────────────────────


def scenes(video, threshold=SCENE_THRESHOLD):
    r = run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(video),
             "-filter:v", f"select='gt(scene,{threshold})',showinfo",
             "-an", "-f", "null", "-"])
    cuts = [round(float(m), 2) for m in re.findall(r"pts_time:([0-9.]+)", r.stderr)]
    return sorted(set(cuts))


def edit_metrics(cuts, duration):
    starts = [0.0] + cuts
    shots = len(starts)
    lens = [round(b - a, 2) for a, b in zip(starts, starts[1:] + [duration])]
    return {
        "shots": shots,
        "cuts": cuts,
        "first_cut_at": cuts[0] if cuts else None,
        "cuts_in_first_3s": len([c for c in cuts if c <= 3.0]),
        "avg_shot_len": round(duration / shots, 2) if shots else None,
        "longest_shot": max(lens) if lens else None,
        "cut_rate_per_10s": round(len(cuts) / duration * 10, 1) if duration else None,
    }


# ─────────────────────────────── кадры ───────────────────────────────


def pick_times(cuts, duration):
    times = [t for t in HOOK_TIMES if t < duration]
    times += [round(c + 0.15, 2) for c in cuts if c + 0.15 < duration]
    if duration > 6:  # добить равномерной сеткой, если склеек мало
        step = max(1.5, duration / 10)
        t = 4.0
        while t < duration - 0.3:
            times.append(round(t, 2))
            t += step
    times.append(round(max(0.0, duration - 0.4), 2))  # финальный кадр = CTA/пэкшот
    uniq = []
    for t in sorted(times):
        if not uniq or t - uniq[-1] > 0.45:
            uniq.append(t)
    if len(uniq) > MAX_FRAMES:  # прореживаем хвост, hook-окно не трогаем
        head = [t for t in uniq if t <= 3.0]
        tail = uniq[len(head):]
        keep = max(1, MAX_FRAMES - len(head))
        idx = [round(i * (len(tail) - 1) / (keep - 1)) for i in range(keep)] if keep > 1 and tail else []
        uniq = head + [tail[i] for i in sorted(set(idx))]
    return uniq


def grab_frames(video, times, frames_dir, width=480):
    frames_dir.mkdir(parents=True, exist_ok=True)
    for f in frames_dir.glob("f*.jpg"):
        f.unlink()
    out = []
    for i, t in enumerate(times):
        dest = frames_dir / f"f{i:03d}.jpg"
        r = run(["ffmpeg", "-y", "-loglevel", "error", "-ss", str(t), "-i", str(video),
                 "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "3", str(dest)])
        if dest.exists() and dest.stat().st_size > 0:
            out.append({"i": i, "t": t, "file": str(dest)})
        elif r.returncode != 0 and i == 0:
            raise RuntimeError(f"ffmpeg не смог достать кадр: {r.stderr.strip()[:160]}")
    return out


def contact_sheet(frames_dir, dest, n, cols=SHEET_COLS):
    if not n:
        return None
    rows = math.ceil(n / cols)
    r = run(["ffmpeg", "-y", "-loglevel", "error", "-start_number", "0",
             "-i", str(frames_dir / "f%03d.jpg"),
             "-frames:v", "1", "-vf", f"scale=360:-2,tile={cols}x{rows}:padding=6:margin=6:color=black",
             "-q:v", "3", str(dest)])
    return str(dest) if dest.exists() and r.returncode == 0 else None


# ─────────────────────────────── звук ───────────────────────────────


def extract_audio(video, wav):
    r = run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(video),
             "-vn", "-ac", "1", "-ar", "16000", str(wav)])
    return wav if wav.exists() and r.returncode == 0 else None


def silence_map(video, duration, db=-30, min_dur=0.35):
    r = run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(video),
             "-af", f"silencedetect=n={db}dB:d={min_dur}", "-f", "null", "-"])
    starts = [float(x) for x in re.findall(r"silence_start: ([0-9.\-]+)", r.stderr)]
    ends = [float(x) for x in re.findall(r"silence_end: ([0-9.]+)", r.stderr)]
    spans = []
    for i, s in enumerate(starts):
        e = ends[i] if i < len(ends) else duration
        spans.append([round(max(0.0, s), 2), round(e, 2)])
    audio_start = 0.0
    if spans and spans[0][0] <= 0.15:
        audio_start = spans[0][1]
    silent = sum(e - s for s, e in spans)
    return {
        "audio_starts_at": round(audio_start, 2),
        "silent_ratio": round(silent / duration, 2) if duration else None,
        "silence_spans": spans[:20],
    }


# ─────────────────────────────── скрипт ───────────────────────────────


def transcribe(wav, lang=None):
    """faster-whisper → whisper CLI → whisper.cpp. Нет ни одного — вернём None."""
    model_size = os.environ.get("ADSPY_WHISPER_MODEL", "base")
    try:
        from faster_whisper import WhisperModel
        model = WhisperModel(model_size, device="auto", compute_type="int8")
        segs, info = model.transcribe(str(wav), language=lang, vad_filter=True)
        out = [{"t0": round(s.start, 2), "t1": round(s.end, 2), "text": s.text.strip()}
               for s in segs if s.text.strip()]
        return out, f"faster-whisper:{model_size}", getattr(info, "language", lang)
    except ImportError:
        pass
    except Exception as e:
        print(f"    faster-whisper упал: {e}", file=sys.stderr)

    if have("whisper"):
        outdir = wav.parent
        cmd = ["whisper", str(wav), "--model", model_size, "--output_format", "json",
               "--output_dir", str(outdir), "--verbose", "False"]
        if lang:
            cmd += ["--language", lang]
        if run(cmd).returncode == 0:
            jf = outdir / f"{wav.stem}.json"
            if jf.exists():
                data = json.loads(jf.read_text(encoding="utf-8"))
                out = [{"t0": round(s["start"], 2), "t1": round(s["end"], 2),
                        "text": s["text"].strip()} for s in data.get("segments", [])]
                return out, f"whisper-cli:{model_size}", data.get("language", lang)

    cpp = os.environ.get("ADSPY_WHISPER_CPP")
    cpp_model = os.environ.get("ADSPY_WHISPER_CPP_MODEL")
    if cpp and cpp_model and Path(cpp).exists():
        r = run([cpp, "-m", cpp_model, "-f", str(wav), "-oj", "-of", str(wav.with_suffix(""))])
        jf = wav.with_suffix(".json")
        if r.returncode == 0 and jf.exists():
            data = json.loads(jf.read_text(encoding="utf-8"))
            out = []
            for s in data.get("transcription", []):
                off = s.get("offsets", {})
                out.append({"t0": round(off.get("from", 0) / 1000, 2),
                            "t1": round(off.get("to", 0) / 1000, 2),
                            "text": s.get("text", "").strip()})
            return out, "whisper.cpp", lang
    return None, None, None


def wpm(transcript, duration):
    if not transcript or not duration:
        return None
    words = sum(len(s["text"].split()) for s in transcript)
    return round(words / duration * 60)


# ─────────────────────────────── супера (OCR) ───────────────────────────────


def ocr_frames(frames, lang="eng"):
    if not have("tesseract"):
        return None
    out, prev = [], None
    for fr in frames:
        r = run(["tesseract", fr["file"], "stdout", "-l", lang, "--psm", "6"])
        if r.returncode != 0:
            continue
        lines = [ln.strip() for ln in r.stdout.splitlines()]
        text = " ".join(ln for ln in lines if len(ln) > 2)
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) < 3 or text == prev:
            continue
        prev = text
        out.append({"t": fr["t"], "text": text[:200]})
    return out


# ─────────────────────────────── сборка ───────────────────────────────


def dissect_one(ad, out, lang=None, ocr_lang="eng", no_audio=False):
    video = Path(ad["local_video"])
    ad_id = ad["ad_id"]
    tech = probe(video)
    cuts = scenes(video)
    edit = edit_metrics(cuts, tech["duration"])
    frames_dir = out / "frames" / ad_id
    frames = grab_frames(video, pick_times(cuts, tech["duration"]), frames_dir)
    sheet = contact_sheet(frames_dir, out / "frames" / f"{ad_id}-sheet.jpg", len(frames))

    audio, transcript, engine, det_lang = {}, None, None, None
    if tech["has_audio"] and not no_audio:
        audio = silence_map(video, tech["duration"])
        wav = extract_audio(video, out / "audio" / f"{ad_id}.wav")
        if wav:
            transcript, engine, det_lang = transcribe(wav, lang)

    supers = ocr_frames(frames, ocr_lang)
    speech_start = transcript[0]["t0"] if transcript else audio.get("audio_starts_at")

    return {
        "ad_id": ad_id,
        "advertiser": ad.get("advertiser"),
        "platform": ad.get("platform"),
        "video": str(video),
        "tech": tech,
        "edit": edit,
        "audio": audio,
        "hook_window": {
            "cuts_in_first_3s": edit["cuts_in_first_3s"],
            "first_cut_at": edit["first_cut_at"],
            "speech_starts_at": speech_start,
            "opens_in_silence": bool(speech_start and speech_start > 1.0),
            "first_words": " ".join(s["text"] for s in (transcript or []) if s["t0"] < 3.5)[:220] or None,
            "first_supers": [s for s in (supers or []) if s["t"] <= 3.0],
            "frames": [f for f in frames if f["t"] <= 3.2],
        },
        "transcript": transcript,
        "transcript_engine": engine,
        "language": det_lang,
        "wpm": wpm(transcript, tech["duration"]),
        "supers": supers,
        "frames": frames,
        "contact_sheet": sheet,
    }


def main():
    ap = argparse.ArgumentParser(description="Разбор скачанных креативов на факты")
    ap.add_argument("-o", "--out", required=True, help="папка проекта")
    ap.add_argument("--ad", action="append", help="конкретный ad_id (можно несколько раз)")
    ap.add_argument("-n", "--limit", type=int, default=0)
    ap.add_argument("--lang", help="язык речи, напр. en/ru (по умолчанию автоопределение)")
    ap.add_argument("--ocr-lang", default="eng", help="языки tesseract, напр. eng+rus")
    ap.add_argument("--no-audio", action="store_true", help="без звука и транскрипта")
    ap.add_argument("--force", action="store_true", help="переразобрать уже разобранные")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        for b, why in (("ffmpeg", "обязателен"), ("ffprobe", "обязателен"),
                       ("tesseract", "опционально: OCR суперов"), ("whisper", "опционально: транскрипт")):
            print(("OK   " if have(b) else "НЕТ  ") + f"{b} — {why}")
        try:
            import faster_whisper  # noqa: F401
            print("OK   faster-whisper — транскрипт")
        except ImportError:
            print("НЕТ  faster-whisper — pip install faster-whisper (иначе транскрипта не будет)")
        sys.exit(0 if have("ffmpeg") and have("ffprobe") else 1)

    if not (have("ffmpeg") and have("ffprobe")):
        sys.exit("нужен ffmpeg (brew install ffmpeg / apt install ffmpeg)")

    out = Path(args.out).expanduser()
    ads_path = out / "ads.json"
    ads = json.loads(ads_path.read_text(encoding="utf-8"))
    (out / "dissect").mkdir(parents=True, exist_ok=True)
    (out / "audio").mkdir(exist_ok=True)

    todo = []
    for a in ads:
        if args.ad and a["ad_id"] not in args.ad:
            continue
        if a.get("duplicate_of"):
            continue
        if not a.get("local_video") or not Path(a["local_video"]).exists():
            continue
        if a.get("dissected") and not args.force and (out / "dissect" / f"{a['ad_id']}.json").exists():
            continue
        todo.append(a)
    if args.limit:
        todo = todo[:args.limit]

    done, failed = 0, []
    for i, a in enumerate(todo, 1):
        print(f"[{i}/{len(todo)}] {a['ad_id']} {a.get('advertiser') or ''}", flush=True)
        try:
            res = dissect_one(a, out, lang=args.lang, ocr_lang=args.ocr_lang, no_audio=args.no_audio)
        except Exception as e:
            failed.append((a["ad_id"], str(e)[:160]))
            print(f"    ✗ {e}")
            continue
        (out / "dissect" / f"{a['ad_id']}.json").write_text(
            json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
        a["dissected"] = True
        a["duration"] = res["tech"]["duration"]
        a["aspect"] = res["tech"]["aspect"]
        done += 1
        t = res["edit"]
        print(f"    {res['tech']['duration']}s {res['tech']['aspect']} | планов {t['shots']} "
              f"| склейки/10с {t['cut_rate_per_10s']} | речь с {res['hook_window']['speech_starts_at']}s "
              f"| транскрипт: {res['transcript_engine'] or 'нет'}")

    ads_path.write_text(json.dumps(ads, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nРазобрано {done} → {out/'dissect'}; кадры в {out/'frames'}")
    for ad_id, err in failed:
        print(f"  ✗ {ad_id} — {err}")


if __name__ == "__main__":
    main()
