# Схема данных

Папка проекта — единица работы. Всё в ней переживает повторные прогоны.

```
<проект>/
  ads.json              база объявлений (главный файл)
  ads.csv               то же таблицей для человека и для таблиц
  facts.md              сводка: лидерборд, динамика, профиль продакшна, крючки
  analysis_input.json   бандл для разбора (читать этот файл, а не десяток мелких)
  REPORT.md             разбор — пишешь ты
  media/<ad_id>.mp4     видео
  thumbs/<ad_id>.jpg    превью
  frames/<ad_id>/f###.jpg   кадры
  frames/<ad_id>-sheet.jpg  контактный лист
  dissect/<ad_id>.json  полный разбор одного ролика
  audio/<ad_id>.wav     дорожка для транскрипта
  raw/                  сырые ответы библиотек (для починки экстракторов)
```

## ads.json — запись объявления

| Поле | Смысл |
|---|---|
| `ad_id` | `meta-<id>` / `tt-<id>` / `ttlib-<id>` — сквозной ключ во всех файлах |
| `platform` | `meta` / `tiktok` |
| `advertiser`, `advertiser_id` | рекламодатель |
| `ad_url` | карточка объявления в библиотеке |
| `is_active`, `started_at`, `ended_at` | статус и даты показа |
| `days_active` | срок жизни в днях — главный сигнал |
| `variants` | объявлений в связке — сигнал бюджета |
| `placements` | facebook / instagram / tiktok |
| `media_type`, `video_url`, `extra_videos`, `thumb_url` | медиа |
| `headline`, `body_text` | тексты объявления |
| `cta_text`, `cta_type`, `link_url`, `landing_domain` | оффер и куда ведёт |
| `reach` | охват, только там, где библиотека его отдаёт (ЕС) |
| `metrics` | CTR/лайки — только TikTok Creative Center |
| `first_captured_at`, `last_seen_at`, `seen_runs` | история наблюдения |
| `local_video`, `bytes`, `creative_hash`, `duplicate_of` | что скачано |
| `dissected`, `duration`, `aspect` | что разобрано |
| `_raw_keys` | какие ключи реально были в ответе библиотеки — подсказка при починке |

Записи не удаляются: пропавшее из библиотеки объявление остаётся в базе со
старым `last_seen_at` и попадает в раздел «выключено» в `facts.md`.

## dissect/<ad_id>.json

`tech` (длительность, размер, формат кадра, fps, звук) · `edit` (планы,
склейки, темп) · `audio` (старт звука, доля тишины) · `hook_window` (всё про
первые 3 секунды, включая кадры) · `transcript` (сегменты с таймкодами) ·
`transcript_engine`, `language`, `wpm` · `supers` (OCR по кадрам) · `frames`
(таймкод → файл) · `contact_sheet`.

Расшифровка каждого поля — в `dissection.md`.

## analysis_input.json

Один файл со всем, что нужно для разбора: по каждому разобранному объявлению —
метаданные, статус, техника, монтаж, hook-окно, текст транскрипта в одну
строку с таймкодами, супера, путь к контактному листу и список кадров с их
таймкодами (ячейки листа идут в том же порядке).
Отсортирован по сроку жизни. Читай его, а не `dissect/*.json` по одному.

## Добавить объявление руками

Ролики из источников без автосбора (Google Ads Transparency, присланные
ссылки) кладутся в `media/<ad_id>.mp4`, а в `ads.json` дописывается запись
минимум с `ad_id`, `platform`, `advertiser`, `local_video`, `media_type:
"video"`, `is_active`. Дальше `spy_dissect.py` и `spy_report.py` подхватят её
как обычную.
