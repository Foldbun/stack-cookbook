// Post-build step: make dist/ compatible with a strict CSP (no 'unsafe-inline' for scripts or styles).
//
// Starlight (and starlight-openapi) emit inline <script> blocks and style="" attributes. This script
//   1. moves every executable inline <script> into a content-hashed file under dist/_astro/ (same tag
//      attributes, same position, so execution order is unchanged),
//   2. replaces every style="..." attribute by a generated class and writes the rules into one
//      stylesheet (dist/_astro/inline-styles.<hash>.css), linked from each page that needs it. The rules
//      repeat the class so they outrank component CSS, and JS that sets element.style (CSSOM) still wins,
//      as it did over inline attributes,
//   3. verifies that nothing inline is left and that no page loads from another origin,
//   4. writes the resulting policy, one line, to csp.txt (for the web server to send as Content-Security-Policy).
//
// Usage: node scripts/csp.mjs [distDir]   (run by `pnpm build`; exits 1 when the check fails)

import { createHash } from 'node:crypto'
import { existsSync, mkdirSync, readdirSync, readFileSync, statSync, writeFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { BRAND } from '../src/lib/brand.ts'

const here = dirname(fileURLToPath(import.meta.url))
const dist = resolve(process.argv[2] ?? join(here, '..', 'dist'))
const cspFile = join(here, '..', 'csp.txt')

/**
 * The policy. 'wasm-unsafe-eval' is needed by Pagefind (search) to instantiate its WebAssembly module.
 * `data:` in img-src is needed by Starlight's own CSS, which draws its icons (heading anchors, copy button,
 * asides, chevrons) as CSS masks of data: SVG images. Images cannot run script.
 */
export const POLICY = [
  "default-src 'self'",
  "script-src 'self' 'wasm-unsafe-eval'",
  "style-src 'self'",
  "img-src 'self' data:",
  "font-src 'self'",
  "connect-src 'self'",
  "object-src 'none'",
  "base-uri 'self'",
  "form-action 'self'",
  "frame-ancestors 'none'",
].join('; ')

const walk = (d) =>
  readdirSync(d).flatMap((f) => {
    const p = join(d, f)
    return statSync(p).isDirectory() ? walk(p) : [p]
  })
const sha = (s) => createHash('sha256').update(s).digest('hex')

const RAW_TEXT = /(<script\b[\s\S]*?<\/script>|<style\b[\s\S]*?<\/style>)/gi
const TAG = /<([a-zA-Z][\w:-]*)((?:\s+[^\s=>"'/]+(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>"']+))?)*)\s*(\/?)>/g
const ATTR = /\s+([^\s=>"'/]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>"']+)))?/g
const NON_EXEC = /\btype\s*=\s*["']?(application\/(ld\+)?json|importmap|speculationrules)/i

const BASE = "/cookbook" // astro.config.mjs base
const assets = join(dist, '_astro')
mkdirSync(assets, { recursive: true })

const rules = new Map() // class -> declarations
const files = walk(dist).filter((f) => f.endsWith('.html'))
const pages = new Map()
let movedScripts = 0
let movedStyles = 0

function decode(v) {
  return v.replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&amp;/g, '&')
}

for (const file of files) {
  const html = readFileSync(file, 'utf8')
  let usesStyles = false
  const out = html
    .split(RAW_TEXT)
    .map((part) => {
      if (/^<script\b/i.test(part)) {
        const m = /^<script\b([^>]*)>([\s\S]*)<\/script>$/i.exec(part)
        if (!m || /\bsrc\s*=/.test(m[1]) || NON_EXEC.test(m[1]) || m[2].trim() === '') return part
        const name = `inline-${sha(m[2]).slice(0, 12)}.js`
        const target = join(assets, name)
        if (!existsSync(target)) writeFileSync(target, m[2].trim() + '\n')
        movedScripts++
        return `<script${m[1]} src="${BASE}/_astro/${name}"></script>`
      }
      if (/^<style\b/i.test(part)) return part // reported by the check below
      return part.replace(TAG, (tag, name, attrs, close) => {
        let cls
        let style
        const kept = []
        for (const a of attrs.matchAll(ATTR)) {
          const [, key, dq, sq, bare] = a
          const val = dq ?? sq ?? bare
          if (key.toLowerCase() === 'style' && val !== undefined) style = decode(val).trim()
          else {
            if (key.toLowerCase() === 'class') cls = val
            kept.push(a[0])
          }
        }
        if (style === undefined) return tag
        const decl = style.replace(/;\s*$/, '')
        const id = `sty-${sha(decl).slice(0, 10)}`
        rules.set(id, decl)
        movedStyles++
        usesStyles = true
        const attrsOut = kept
          .filter((s) => !/^\s+class\s*=/i.test(s))
          .join('')
        const classAttr = ` class="${cls ? cls + ' ' : ''}${id}"`
        return `<${name}${attrsOut}${classAttr}${close ? ' /' : ''}>`
      })
    })
    .join('')
  pages.set(file, { html: out, usesStyles })
}

// Code highlighting. Expressive Code colours a token through custom properties on its span (style="--0:#…;--1:#…") and
// a stylesheet rule that selects `span[style^='--']:not([class])`. Step 2 above turns that attribute into a class, so the
// rule no longer matches and every code block would be one colour. These rules are the same three, selecting the
// generated class instead (`--0` = dark theme, `--1` = light theme, with the same theme conditions).
const TOKEN = ":where(span[class^='sty-'],span[class*=' sty-'])"
const tokenDecl = (n) =>
  `{color:var(--${n},inherit);background-color:var(--${n}bg,transparent);font-style:var(--${n}fs,inherit);font-weight:var(--${n}fw,inherit);text-decoration:var(--${n}td,inherit)}`
const CODE_TOKENS = [
  `.expressive-code .ec-line ${TOKEN},:root:not([data-theme='dark']) .expressive-code[data-theme='dark'] .ec-line ${TOKEN}${tokenDecl(0)}`,
  `:root:not([data-theme='dark']) .expressive-code .ec-line ${TOKEN}${tokenDecl(1)}`,
  `:root[data-theme='light'] .expressive-code:not([data-theme='dark']) .ec-line ${TOKEN},.expressive-code[data-theme='light'] .ec-line ${TOKEN}${tokenDecl(1)}`,
].join('\n')

let sheetHref = null
if (rules.size > 0) {
  const css = [...rules]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([id, decl]) => `.${id}.${id}.${id}{${decl}}`)
    .join('\n') + '\n' + CODE_TOKENS
  const name = `inline-styles.${sha(css).slice(0, 10)}.css`
  writeFileSync(join(assets, name), css + '\n')
  sheetHref = `${BASE}/_astro/${name}`
}

const problems = []
for (const [file, { html, usesStyles }] of pages) {
  let out = html
  if (usesStyles && sheetHref) out = out.replace('</head>', `<link rel="stylesheet" href="${sheetHref}"></head>`)
  writeFileSync(file, out)

  const rel = file.slice(dist.length)
  const bare = out.split(RAW_TEXT)
  for (const part of bare) {
    if (/^<script\b/i.test(part)) {
      const m = /^<script\b([^>]*)>([\s\S]*)<\/script>$/i.exec(part)
      if (m && !/\bsrc\s*=/.test(m[1]) && !NON_EXEC.test(m[1]) && m[2].trim() !== '') problems.push(`${rel}: inline script`)
    } else if (/^<style\b/i.test(part)) problems.push(`${rel}: inline <style>`)
    else {
      for (const t of part.matchAll(TAG)) {
        if (/\sstyle\s*=/i.test(t[2])) problems.push(`${rel}: style attribute on <${t[1]}>`)
        if (/\son[a-z]+\s*=/i.test(t[2])) problems.push(`${rel}: event handler attribute on <${t[1]}>`)
        for (const u of t[2].matchAll(/\b(?:src|href|action|poster|srcset)\s*=\s*["']?\s*(javascript:|data:|https?:\/\/|\/\/)([^"'\s>]*)/gi)) {
          // Outbound <a href> links and canonical/alternate/sitemap URLs are not resource loads.
          const isLoad = /^(script|img|iframe|video|audio|source|embed|object|form)$/i.test(t[1]) || /\brel\s*=\s*["']?(stylesheet|preload|modulepreload|icon|shortcut|manifest)/i.test(t[2])
          if (isLoad && !(u[1].startsWith('http') && u[2].startsWith(new URL(BRAND.siteUrl).host))) problems.push(`${rel}: <${t[1]}> loads ${u[1]}${u[2].slice(0, 40)}`)
        }
      }
    }
  }
}

writeFileSync(cspFile, POLICY + '\n')
console.log(`csp: ${files.length} pages, ${movedScripts} inline scripts moved, ${movedStyles} style attributes moved to ${rules.size} classes`)
console.log(`csp: wrote ${cspFile}`)
if (problems.length) {
  console.error(`csp: ${problems.length} problem(s):\n  ` + [...new Set(problems)].slice(0, 30).join('\n  '))
  process.exit(1)
}
