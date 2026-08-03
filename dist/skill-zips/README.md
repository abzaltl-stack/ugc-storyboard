# Skill zips для загрузки в аккаунт claude.ai

Архивы для кнопки **Add** в Settings → Skills (десктоп-приложение или claude.ai).
После загрузки скилл появляется по `/` в любом новом чате.

## Что загружать (11 архивов)

**Remotion — один архив на всё:**
- `remotion-best-practices.zip` — роутер, внутри уже лежат остальные 10 Remotion-скиллов
  (create, docs, render, captions, markup, multimedia, interactivity, maps, saas, upgrade)

**3D и анимация — 10 архивов:**
- `threejs-webgl.zip`
- `react-three-fiber.zip`
- `babylonjs-engine.zip`
- `gsap-scrolltrigger.zip`
- `motion-framer.zip`
- `lottie-animations.zip`
- `animejs.zip`
- `react-spring-physics.zip`
- `animated-component-libraries.zip`
- `scroll-reveal-libraries.zip`

Остальные `remotion-*.zip` в этой папке — отдельные копии на случай, если понадобится
загрузить какой-то один скилл без роутера. Вместе с `remotion-best-practices.zip`
их грузить не нужно, будут дубли.

## Пересобрать

```bash
OUT=dist/skill-zips; rm -rf "$OUT"; mkdir -p "$OUT"; TMP=$(mktemp -d)
for d in .claude/skills/*/; do
  name=$(basename "$d")
  cp -rL "$d" "$TMP/$name"
  (cd "$TMP" && zip -qr "$OUT/$name.zip" "$name")
done
rm -rf "$TMP"
```

`cp -rL` обязателен: `.claude/skills/remotion-*` — это симлинки на `.agents/skills/`,
без разыменования в архив попадут битые ссылки.
