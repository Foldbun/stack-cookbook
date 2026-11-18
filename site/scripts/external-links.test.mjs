// Links that leave the Cookbook frame open in a new tab. Unit tests of isCrossApp() and checks on the built pages
// (run `pnpm build` first; `pnpm test` runs this file with node:test).
import assert from 'node:assert/strict'
import { existsSync, readFileSync, readdirSync, statSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'
import { isCrossApp } from '../src/lib/external.mjs'

const here = dirname(fileURLToPath(import.meta.url))
const dist = join(here, '..', 'dist')

test('isCrossApp: Cookbook pages and anchors stay in the same tab', () => {
  for (const h of ['/cookbook/', '/cookbook/zh/', '/cookbook/use-cases/x/', '/cookbook/zh/use-cases/x/#problem', '/cookbook',
    'https://stack.foldbun.com/cookbook/', 'https://stack.foldbun.com/cookbook/zh/use-cases/x/', '#problem', '', 'mailto:someone@example.org',
    'other/', '../other/', undefined, 'javascript:void(0)']) {
    assert.equal(isCrossApp(h), false, String(h))
  }
})

test('isCrossApp: other apps, other hosts and third parties open in a new tab', () => {
  for (const h of ['/docs/', '/zh/docs/models/', '/pricing/', '/models/', '/console/', '/playground/', '/cookbookx/', '/cookbook-old/',
    'https://stack.foldbun.com/', 'https://stack.foldbun.com/docs/', 'https://stack.foldbun.com/zh/docs/x/', 'https://stack.foldbun.com/console/',
    'https://stack.foldbun.com/playground/?m=1', 'https://foldbun.com/terms/', 'https://id.foldbun.com/', 'https://annotation.foldbun.com/zh/',
    'https://status.foldbun.com/', 'https://api.stack.foldbun.com/v1/', 'https://github.com/Foldbun/stack-cookbook/blob/main/x.py',
    'https://example.org/data.csv', '//example.org/x', 'http://example.org/']) {
    assert.equal(isCrossApp(h), true, h)
  }
})

test('isCrossApp: a different site URL changes what is "the same host"', () => {
  assert.equal(isCrossApp('https://example.test/cookbook/x/', 'https://example.test'), false)
  assert.equal(isCrossApp('https://stack.foldbun.com/cookbook/x/', 'https://example.test'), true)
})

// ---- built pages ----
const walk = (d) => readdirSync(d).flatMap((f) => (statSync(join(d, f)).isDirectory() ? walk(join(d, f)) : [join(d, f)]))
assert.ok(existsSync(dist), 'dist/ is missing: run `pnpm build` before `pnpm test`')
const pages = walk(dist).filter((f) => f.endsWith('.html') && !/[\\/]pagefind[\\/]/.test(f.slice(dist.length)))
const anchors = (html) => [...html.matchAll(/<a\b([^>]*)>/g)].map((m) => {
  const attr = (n) => new RegExp(`\\s${n}="([^"]*)"`).exec(m[1])?.[1]
  return { href: attr('href')?.replace(/&amp;/g, '&'), target: attr('target'), rel: attr('rel'), end: m.index + m[0].length }
}).filter((a) => a.href !== undefined)

test('built pages: every cross-app link has target=_blank, rel=noopener, an icon and the hidden note; no other link has a target', () => {
  let cross = 0
  for (const file of pages) {
    const html = readFileSync(file, 'utf8')
    const zh = /[\\/]zh[\\/]/.test(file.slice(dist.length))
    const note = zh ? '（在新标签页打开）' : '(opens in a new tab)'
    for (const a of anchors(html)) {
      if (!isCrossApp(a.href)) { assert.equal(a.target, undefined, `${file}: ${a.href} must stay in the tab`); continue }
      cross++
      assert.equal(a.target, '_blank', `${file}: ${a.href}`)
      assert.ok(/noopener/.test(a.rel ?? ''), `${file}: ${a.href} rel`)
      const body = html.slice(a.end, html.indexOf('</a>', a.end))
      assert.ok(body.includes('class="fb-ic ext-ic"') && body.includes('aria-hidden="true"'), `${file}: ${a.href} icon`)
      assert.ok(body.includes(`<span class="vh"> ${note}</span>`), `${file}: ${a.href} hidden note`)
    }
  }
  assert.ok(cross > 50, `expected many cross-app links, found ${cross}`)
})

test('built use case page: links to Stack docs, console and GitHub open in a new tab, links to another Cookbook page do not', () => {
  const ucDir = join(dist, 'use-cases')
  const slugs = readdirSync(ucDir).filter((d) => existsSync(join(ucDir, d, 'index.html')))
  const sample = { docs: null, console: null, github: null, cookbook: null }
  for (const slug of slugs) {
    for (const a of anchors(readFileSync(join(ucDir, slug, 'index.html'), 'utf8'))) {
      const body = a.href
      if (/^https:\/\/stack\.foldbun\.com\/(zh\/)?docs\//.test(body)) sample.docs ??= a
      else if (/^(https:\/\/stack\.foldbun\.com)?\/console\//.test(body)) sample.console ??= a
      else if (body.startsWith('https://github.com/')) sample.github ??= a
      else if (body.startsWith('/cookbook/use-cases/') && body.endsWith('/')) sample.cookbook ??= a
    }
  }
  for (const k of ['docs', 'github']) { assert.ok(sample[k], `no ${k} link found in the use case pages`); assert.equal(sample[k].target, '_blank', `${k}: ${sample[k].href}`) }
  if (sample.console) assert.equal(sample.console.target, '_blank')
  assert.ok(sample.cookbook, 'no link to another Cookbook page found in the use case pages')
  assert.equal(sample.cookbook.target, undefined, sample.cookbook.href)
})
