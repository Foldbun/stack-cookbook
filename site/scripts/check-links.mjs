// Link check over the built HTML: every internal link, script, stylesheet, image and favicon must resolve to a file in
// dist/ under the base path (/cookbook/), and every #fragment must name an id of the target page.
// External links (https://...) are counted, not fetched. Usage: node scripts/check-links.mjs [distDir]  (exit 1 on a problem)
import { existsSync, readFileSync, readdirSync, statSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const BASE = '/cookbook/'
const dist = resolve(process.argv[2] ?? join(dirname(fileURLToPath(import.meta.url)), '..', 'dist'))
const walk = (d) => readdirSync(d).flatMap((f) => (statSync(join(d, f)).isDirectory() ? walk(join(d, f)) : [join(d, f)]))
const pages = walk(dist).filter((f) => f.endsWith('.html'))
const idCache = new Map()
const idsOf = (file) => {
  if (!idCache.has(file)) idCache.set(file, new Set([...readFileSync(file, 'utf8').matchAll(/\sid="([^"]+)"/g)].map((m) => m[1])))
  return idCache.get(file)
}

const ATTR = /<(a|link|script|img|source)\b[^>]*?\s(href|src|srcset)="([^"]*)"/g
const problems = []
let internal = 0
let external = 0
for (const page of pages) {
  const html = readFileSync(page, 'utf8')
  const here = BASE + page.slice(dist.length + 1).replace(/index\.html$/, '')
  for (const [, tag, attr, raw] of html.matchAll(ATTR)) {
    const urls = attr === 'srcset' ? raw.split(',').map((s) => s.trim().split(/\s+/)[0]) : [raw]
    for (const u of urls) {
      if (!u || /^(mailto:|tel:|data:|javascript:)/.test(u)) continue
      if (/^(https?:)?\/\//.test(u)) {
        external++
        continue
      }
      internal++
      const [pathAndQuery, frag] = u.split('#')
      const path = pathAndQuery.split('?')[0]
      const abs = path === '' ? here : path.startsWith('/') ? path : new URL(path, `https://x${here}`).pathname
      if (!abs.startsWith(BASE)) {
        problems.push(`${here}: <${tag}> ${attr}="${u}" points outside ${BASE}`)
        continue
      }
      let file = join(dist, abs.slice(BASE.length))
      if (abs.endsWith('/')) file = join(file, 'index.html')
      if (!existsSync(file) || !statSync(file).isFile()) {
        problems.push(`${here}: <${tag}> ${attr}="${u}" does not resolve (${file.slice(dist.length)})`)
        continue
      }
      if (frag && tag === 'a' && file.endsWith('.html') && !idsOf(file).has(decodeURIComponent(frag))) problems.push(`${here}: link "${u}" names a missing anchor`)
    }
  }
}
console.log(`check-links: ${pages.length} pages, ${internal} internal references, ${external} external (not fetched)`)
if (problems.length) {
  console.error(`check-links: ${problems.length} problem(s):\n  ` + [...new Set(problems)].slice(0, 40).join('\n  '))
  process.exit(1)
}
