// Post-build step: adds to dist/sitemap-0.xml what the sitemap integration does not write.
//   <lastmod>   the time of the last commit touching the page sources (src/generated/lastmod.json, written by
//               sync-usecases.mjs); a page without a time gets none (never the build time),
//   hreflang="zh"   next to the zh-CN alternate of every page, the same pair as in the page <head>.
// Usage: node scripts/sitemap-extras.mjs [distDir]   (run by `pnpm build`, before csp.mjs)
import { readFileSync, writeFileSync, existsSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { BRAND } from '../src/lib/brand.ts'

const here = dirname(fileURLToPath(import.meta.url))
const dist = resolve(process.argv[2] ?? join(here, '..', 'dist'))
const lastmodFile = join(here, '..', 'src/generated/lastmod.json')
const lastmod = existsSync(lastmodFile) ? JSON.parse(readFileSync(lastmodFile, 'utf8')) : {}
const file = join(dist, 'sitemap-0.xml')

let withTime = 0
let withZh = 0
const xml = readFileSync(file, 'utf8').replace(/<url>[\s\S]*?<\/url>/g, (block) => {
  const loc = /<loc>([^<]+)<\/loc>/.exec(block)?.[1]
  if (!loc) return block
  const time = lastmod[new URL(loc).pathname]
  let out = block
  if (time && !out.includes('<lastmod>')) {
    out = out.replace('</loc>', `</loc><lastmod>${time}</lastmod>`)
    withTime++
  }
  const zh = /<xhtml:link rel="alternate" hreflang="zh-CN" href="([^"]+)"\/>/.exec(out)
  if (zh && !out.includes('hreflang="zh"')) {
    out = out.replace(zh[0], `${zh[0]}<xhtml:link rel="alternate" hreflang="zh" href="${zh[1]}"/>`)
    withZh++
  }
  return out
})
writeFileSync(file, xml)
console.log(`sitemap-extras: ${withTime} lastmod, ${withZh} hreflang zh (${BRAND.siteUrl})`)
