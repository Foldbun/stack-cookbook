// Draws the default link-preview images (1200x630 PNG) into public/og/. The PNGs are committed; run this script only when
// the brand or the wording changes:  node scripts/make-og.mjs
//
// Needs `sharp` (a dependency of the site) and an installed font with Latin and Simplified Chinese glyphs (the default
// below is Noto Sans CJK SC, SIL OFL licence; pass another family name as the first argument). Text is set in the SVG and
// rasterised by sharp, so the result depends on that font; this is why the output is checked in and not built in CI.
import { readFileSync, mkdirSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import sharp from 'sharp'
import { BRAND } from '../src/lib/brand.ts'

const here = dirname(fileURLToPath(import.meta.url))
const out = join(here, '..', 'public', 'og')
const FONT = process.argv[2] ?? 'Noto Sans CJK SC'
const logo = readFileSync(join(here, '..', 'src/assets/brand/foldbun-stack-color-wheat-dark.svg')).toString('base64')

const COPY = {
  'en-US': { sub: 'Runnable use cases for the vision model API', foot: 'Problem, pipeline, code in several languages, measured results and cost' },
  'zh-CN': { sub: '视觉模型 API 的可运行用例', foot: '问题、管线、多种语言的代码、实测结果与费用' },
}

const svg = (c) => `<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="630" viewBox="0 0 1200 630">
  <rect width="1200" height="630" fill="#222722"/>
  <rect x="0" y="618" width="1200" height="12" fill="${BRAND.color}"/>
  <image x="80" y="80" width="96" height="96" href="data:image/svg+xml;base64,${logo}"/>
  <text x="200" y="146" font-family="${FONT}" font-size="44" font-weight="700" fill="#f3efe6">${BRAND.name}</text>
  <text x="80" y="360" font-family="${FONT}" font-size="132" font-weight="700" fill="#f3efe6">Cookbook</text>
  <text x="80" y="450" font-family="${FONT}" font-size="46" fill="#e3b88f">${c.sub}</text>
  <text x="80" y="560" font-family="${FONT}" font-size="28" fill="#aeb4aa">${c.foot}</text>
</svg>`

mkdirSync(out, { recursive: true })
for (const [lang, copy] of Object.entries(COPY)) {
  const file = join(out, `cookbook.${lang}.png`)
  writeFileSync(file, await sharp(Buffer.from(svg(copy))).png({ compressionLevel: 9, palette: true }).toBuffer())
  console.log(`wrote ${file}`)
}
