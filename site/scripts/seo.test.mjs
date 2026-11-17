// SEO checks on the built site (run `pnpm build` first; `pnpm test` runs this file with node:test).
// Every indexable page needs a unique <title> of at most 60 characters, a description, an og:image that exists in dist/,
// matching Twitter tags, hreflang alternates, and valid JSON-LD (TechArticle or CollectionPage, plus BreadcrumbList).
// The sitemap carries a <lastmod> for every URL when the checkout has its full history.
import assert from 'node:assert/strict'
import { existsSync, readFileSync, readdirSync, statSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'
import { lastCommitTime } from './lastmod.mjs'
import { BRAND } from '../src/lib/brand.ts'

const here = dirname(fileURLToPath(import.meta.url))
const dist = join(here, '..', 'dist')
const BASE = '/cookbook'
const decode = (s) => s.replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&')

const walk = (d) => readdirSync(d).flatMap((f) => (statSync(join(d, f)).isDirectory() ? walk(join(d, f)) : [join(d, f)]))
assert.ok(existsSync(dist), 'dist/ is missing: run `pnpm build` before `pnpm test`')

const pages = walk(dist)
  .filter((f) => f.endsWith('.html') && !/(^|[\\/])(404\.html|pagefind[\\/])/.test(f.slice(dist.length)))
  .map((file) => {
    const html = readFileSync(file, 'utf8')
    const head = /<head>([\s\S]*?)<\/head>/.exec(html)?.[1] ?? ''
    const rel = file.slice(dist.length)
    const meta = (attr, name) => {
      const m = new RegExp(`<meta\\s+${attr}="${name}"\\s+content="([^"]*)"`).exec(head)
      return m ? decode(m[1]) : null
    }
    const jsonld = [...head.matchAll(/<script type="application\/ld\+json">([\s\S]*?)<\/script>/g)].map((m) => JSON.parse(m[1]))
    return {
      rel,
      title: decode(/<title>([\s\S]*?)<\/title>/.exec(head)?.[1] ?? ''),
      description: meta('name', 'description'),
      ogTitle: meta('property', 'og:title'),
      ogImage: meta('property', 'og:image'),
      twImage: meta('name', 'twitter:image'),
      hreflangs: [...head.matchAll(/<link rel="alternate" hreflang="([^"]+)"/g)].map((m) => m[1]),
      jsonld,
      types: jsonld.flatMap((d) => (d['@graph'] ?? [d]).map((n) => n['@type'])),
    }
  })

test('the site has the expected pages (8 use cases and a home page, in two languages)', () => {
  assert.ok(pages.length >= 18, `only ${pages.length} pages found`)
})

test('every page has a unique title of at most 60 characters', () => {
  const seen = new Map()
  for (const p of pages) {
    assert.ok(p.title.length > 0, `${p.rel}: no <title>`)
    assert.ok([...p.title].length <= 60, `${p.rel}: title is ${[...p.title].length} characters: ${p.title}`)
    assert.ok(!seen.has(p.title), `${p.rel}: title duplicates ${seen.get(p.title)}: ${p.title}`)
    seen.set(p.title, p.rel)
    const parts = p.title.split(' | ')
    assert.equal(new Set(parts).size, parts.length, `${p.rel}: a title part is repeated: ${p.title}`)
  }
})

test('every page has a description and Open Graph / Twitter tags that match', () => {
  for (const p of pages) {
    assert.ok(p.description && [...p.description].length >= 15, `${p.rel}: description missing or too short`)
    assert.ok(p.ogTitle, `${p.rel}: no og:title`)
    assert.ok(p.title.startsWith(p.ogTitle), `${p.rel}: og:title is not the start of the title`)
    assert.ok(p.ogImage, `${p.rel}: no og:image`)
    assert.equal(p.twImage, p.ogImage, `${p.rel}: twitter:image differs from og:image`)
  }
})

test('og:image is an absolute URL of a file in dist/, one per language', () => {
  for (const p of pages) {
    const url = new URL(p.ogImage)
    assert.equal(url.origin, BRAND.siteUrl, `${p.rel}: og:image is on another origin`)
    assert.ok(url.pathname.startsWith(`${BASE}/og/`), `${p.rel}: og:image path ${url.pathname}`)
    assert.ok(existsSync(join(dist, url.pathname.slice(BASE.length))), `${p.rel}: ${url.pathname} is not in dist/`)
    const zh = p.rel.startsWith('/zh/')
    assert.ok(url.pathname.endsWith(zh ? 'zh-CN.png' : 'en-US.png'), `${p.rel}: wrong language of og:image ${url.pathname}`)
  }
})

test('pages are linked to their other language, with hreflang zh next to zh-CN', () => {
  for (const p of pages) for (const l of ['en-US', 'zh-CN', 'zh', 'x-default']) assert.ok(p.hreflangs.includes(l), `${p.rel}: no hreflang ${l}`)
})

test('every page has valid JSON-LD with a page node and a BreadcrumbList', () => {
  for (const p of pages) {
    assert.ok(p.jsonld.length > 0, `${p.rel}: no JSON-LD`)
    for (const d of p.jsonld) assert.equal(d['@context'], 'https://schema.org', `${p.rel}: @context`)
    assert.ok(p.types.includes('BreadcrumbList'), `${p.rel}: no BreadcrumbList`)
    const home = p.rel === '/index.html' || p.rel === '/zh/index.html'
    assert.ok(p.types.includes(home ? 'CollectionPage' : 'TechArticle'), `${p.rel}: no ${home ? 'CollectionPage' : 'TechArticle'} (${p.types})`)
    for (const n of p.jsonld.flatMap((d) => d['@graph'] ?? [d])) {
      if (n['@type'] === 'BreadcrumbList') {
        assert.ok(n.itemListElement.length >= 2, `${p.rel}: breadcrumb too short`)
        n.itemListElement.forEach((it, i) => {
          assert.equal(it.position, i + 1, `${p.rel}: breadcrumb positions`)
          assert.ok(it.name && new URL(it.item), `${p.rel}: breadcrumb item`)
        })
      } else {
        assert.ok(n.url && n.description && n.inLanguage && n.image, `${p.rel}: ${n['@type']} lacks url, description, inLanguage or image`)
        assert.equal(n.publisher?.['@id'], 'https://foldbun.com/#organization', `${p.rel}: publisher`)
        if (n['@type'] === 'TechArticle') assert.ok(n.headline && n.headline.length <= 110, `${p.rel}: headline`)
      }
    }
  }
})

test('the sitemap lists every page with a lastmod taken from git history', () => {
  const xml = readFileSync(join(dist, 'sitemap-0.xml'), 'utf8')
  const urls = [...xml.matchAll(/<url>([\s\S]*?)<\/url>/g)].map((m) => m[1])
  assert.equal(urls.length, pages.length, 'the sitemap and the built pages differ in number')
  const root = resolve(here, '..', '..')
  if (lastCommitTime(root, ['.']) === null) return // no full history here (shallow clone or no repository): no lastmod is correct
  for (const u of urls) {
    const loc = /<loc>([^<]+)<\/loc>/.exec(u)[1]
    const time = /<lastmod>([^<]+)<\/lastmod>/.exec(u)?.[1]
    assert.ok(time, `${loc}: no <lastmod>`)
    assert.match(time, /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/, `${loc}: lastmod format`)
    assert.ok(Date.parse(time) <= Date.now(), `${loc}: lastmod is in the future`)
    assert.match(u, /hreflang="zh"/, `${loc}: no hreflang zh`)
  }
})
