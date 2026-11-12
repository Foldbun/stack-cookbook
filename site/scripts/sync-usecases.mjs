// Generates the content tree of the site from the repository sources. Run by `pnpm build` and `pnpm dev`.
//
// Inputs (read-only):  use-cases/*/usecase.toml, README.md, README.zh-CN.md, the language directories, expected/*.json,
//                      assets referenced by the READMEs, harness/languages.toml.
// Outputs (git-ignored): site/src/content/docs/**   one English and one Chinese MDX page per use case, plus both home pages
//                        site/src/generated/usecases.json   the use case x language matrix data
//                        site/public/usecases/<id>/**       figures and images the READMEs reference
//
// Rules shared with `harness snippets` (harness/snippets.py): region markers in source files, the marker blocks
// (`code`, `metrics`, `cost`, `figure`) and the section anchors. Code is read from the source files, never from the
// copies that `harness snippets` expands into the READMEs. Neither the use cases nor the languages are listed here.

import { existsSync, mkdirSync, readFileSync, readdirSync, rmSync, writeFileSync, copyFileSync, statSync } from 'node:fs'
import { dirname, join, relative, resolve, extname, basename } from 'node:path'
import { fileURLToPath } from 'node:url'
import { parse } from 'smol-toml'
import { T } from '../src/i18n.ts'
import { BASE, REPO, docsUrl, localizeDocsLinks, playgroundUrl } from '../src/lib/site.ts'

const here = dirname(fileURLToPath(import.meta.url))
const SITE_DIR = resolve(here, '..')
const ROOT = resolve(SITE_DIR, '..')
const OUT_DOCS = join(SITE_DIR, 'src/content/docs')
const OUT_DATA = join(SITE_DIR, 'src/generated')
const OUT_PUBLIC = join(SITE_DIR, 'public/usecases')

// ---- rules shared with harness/snippets.py ------------------------------------------------------------------------
const SOURCE_EXT = new Set(['.py', '.sh', '.ts', '.go'])
const CLIENT_FILES = new Set(['stack_client.py', 'coco_rle.py', 'stackClient.ts', 'client.go', 'rle.go'])
const BLOCK = /<!-- (code|metrics|cost|usecases)(?::\s*([^\s>]+))? -->\n[\s\S]*?<!-- \/\1 -->/g
const FIGURE = /<!-- figure: ([^\s>]+) -->\n?(!\[[^\]]*\]\([^)\s]+\)\n?)?/g
const REGION_START = /^\s*(?:#|\/\/)\s*#?region\s+([\w.-]+)\s*$/
const REGION_END = /^\s*(?:#|\/\/)\s*#?endregion\b/
const ANCHOR = /^(#{1,6} .*?)[ \t]*<!-- #([a-z0-9-]+) -->[ \t]*$/gm

// Display names and code fence languages. Anything not listed falls back to the capitalised key and the key itself.
const LANG_NAME = { curl: 'curl', python: 'Python', typescript: 'TypeScript', go: 'Go', rust: 'Rust', java: 'Java', csharp: 'C#' }
const FENCE = { curl: 'bash', typescript: 'ts', csharp: 'csharp' }
const langName = (k) => LANG_NAME[k] ?? k.charAt(0).toUpperCase() + k.slice(1)
const fence = (k) => FENCE[k] ?? k

const walk = (d) =>
  readdirSync(d, { withFileTypes: true })
    .flatMap((e) => (e.isDirectory() ? (['node_modules', '.venv', '__pycache__'].includes(e.name) ? [] : walk(join(d, e.name))) : [join(d, e.name)]))
    .sort()

function dedent(lines) {
  const indents = lines.filter((l) => l.trim()).map((l) => l.match(/^[ \t]*/)[0].length)
  const n = indents.length ? Math.min(...indents) : 0
  return lines.map((l) => (l.trim() ? l.slice(n) : '')).join('\n')
}

function regionsOf(path) {
  const out = {}
  let current = null
  let buf = []
  for (const line of readFileSync(path, 'utf8').split('\n')) {
    if (current === null) {
      const m = REGION_START.exec(line)
      if (m) [current, buf] = [m[1], []]
    } else if (REGION_END.test(line)) {
      out[current] = dedent(buf).replace(/^\n+|\n+$/g, '')
      current = null
    } else buf.push(line)
  }
  return out
}

const sourceFiles = (dir) => (existsSync(dir) ? walk(dir).filter((p) => SOURCE_EXT.has(extname(p)) && !CLIENT_FILES.has(basename(p))) : [])

// ---- repository data ----------------------------------------------------------------------------------------------
const languagesToml = parse(readFileSync(join(ROOT, 'harness/languages.toml'), 'utf8'))
const LANG_ORDER = Object.keys(languagesToml)

const useCases = readdirSync(join(ROOT, 'use-cases'), { withFileTypes: true })
  .filter((e) => e.isDirectory() && existsSync(join(ROOT, 'use-cases', e.name, 'usecase.toml')))
  .map((e) => {
    const dir = join(ROOT, 'use-cases', e.name)
    const meta = parse(readFileSync(join(dir, 'usecase.toml'), 'utf8'))
    const languages = [...meta.languages].sort((a, b) => LANG_ORDER.indexOf(a) - LANG_ORDER.indexOf(b))
    return { id: e.name, num: e.name.split('-')[0], dir, meta, languages }
  })
  .sort((a, b) => (a.meta.order ?? 0) - (b.meta.order ?? 0) || a.id.localeCompare(b.id))

const ghBase = (uc) => `${REPO}/blob/main/use-cases/${uc.id}/`
const ghLink = (uc, rel) => {
  const url = new URL(rel, ghBase(uc))
  if (rel.endsWith('/')) url.pathname = url.pathname.replace('/blob/', '/tree/')
  return url.href
}

// ---- generated blocks ---------------------------------------------------------------------------------------------
const esc = (s) => String(s).replace(/[{}]/g, (c) => `\\${c}`).replace(/</g, '&lt;')
const fmt = (v) => (typeof v === 'number' && !Number.isInteger(v) ? v.toLocaleString('en-US', { maximumSignificantDigits: 6 }) : String(v))
const readJson = (uc, rel) => {
  const p = join(uc.dir, rel)
  return existsSync(p) ? JSON.parse(readFileSync(p, 'utf8')) : null
}

function codeBlock(uc, arg, lang) {
  // `NAME@LANG` is a region only one language has (same rule as harness/snippets.py): one tab, no error for the others.
  const [name, only] = arg.split('@')
  if (only && !uc.languages.includes(only)) throw new Error(`${uc.id}: '${arg}': ${only} is not a language of this use case`)
  const tabs = []
  const missing = []
  for (const l of only ? [only] : uc.languages) {
    const found = sourceFiles(join(uc.dir, l))
      .map((f) => [f, regionsOf(f)])
      .find(([, r]) => name in r)
    if (!found) {
      missing.push(l)
      continue
    }
    const rel = relative(uc.dir, found[0]).split('\\').join('/')
    tabs.push(
      `<TabItem label="${langName(l)}">\n\n${T[lang]['uc.sourceFile']}: [\`${rel}\`](${ghLink(uc, rel)})\n\n\`\`\`${fence(l)}\n${found[1][name]}\n\`\`\`\n\n</TabItem>`,
    )
  }
  if (missing.length) throw new Error(`${uc.id}: region '${name}' is missing in ${missing.join(', ')}`)
  return `<Tabs syncKey="lang">\n${tabs.join('\n')}\n</Tabs>`
}

function metricsBlock(uc, rel, lang) {
  const doc = readJson(uc, rel)
  if (doc === null) return ''
  const flat = []
  const walkJson = (prefix, v) => {
    if (v && typeof v === 'object' && !Array.isArray(v)) for (const [k, x] of Object.entries(v)) walkJson(prefix ? `${prefix}.${k}` : k, x)
    else flat.push([prefix, v])
  }
  walkJson('', doc)
  const t = T[lang]
  return `| ${t['metrics.metric']} | ${t['metrics.value']} |\n|---|---|\n${flat.map(([k, v]) => `| \`${k}\` | ${esc(fmt(v))} |`).join('\n')}`
}

function costBlock(uc, lang) {
  const t = T[lang]
  const rows = []
  for (const size of ['smoke', 'full']) {
    const doc = readJson(uc, `expected/${size}.json`)
    if (doc?.ledger) rows.push(`| ${size} | ${doc.ledger.calls} | ${fmt(doc.ledger.listTotal)} | ${fmt(doc.ledger.chargedTotal)} |`)
  }
  if (!rows.length) return ''
  return `| ${t['cost.run']} | ${t['cost.calls']} | ${t['cost.list']} | ${t['cost.charged']} |\n|---|---|---|---|\n${rows.join('\n')}`
}

/** "Run it" tabs: one per language, from usecase.toml and harness/languages.toml. Commands run from the repository root. */
function runBlock(uc, lang) {
  const t = T[lang]
  const clean = (cmd) => cmd.replaceAll('{dir}', '.').replace(/\s(--project|-C|--dir) \.(?=\s|$)/g, '')
  const smoke = uc.meta.sizes?.smoke
  const full = uc.meta.sizes?.full
  const cap = smoke?.budget_credits
  const tabs = uc.languages.map((l) => {
    const cfg = languagesToml[l] ?? {}
    const run = (m) => `${clean(cfg.run ?? '')} --manifest ../${m} --out out`
    const lines = ['export FOLDBUN_API_KEY="fbs_..."']
    if (cap) lines.push(`export FOLDBUN_MAX_CREDITS=${cap}`)
    lines.push(`cd use-cases/${uc.id}/${l}`)
    if (cfg.setup) lines.push(clean(cfg.setup))
    if (smoke?.manifest) lines.push(run(smoke.manifest))
    if (full?.manifest) lines.push('', `# ${t['uc.fullSet']}: ${t['uc.fullSetNote']}`, run(full.manifest))
    const min = cfg.min ? `\n${t['uc.requires']}: ${cfg.min}\n` : ''
    return `<TabItem label="${langName(l)}">\n${min}\n\`\`\`bash\n${lines.join('\n')}\n\`\`\`\n\n</TabItem>`
  })
  return `**${t['uc.runTitle']}**\n\n${t['uc.runIntro']}\n\n<Tabs syncKey="lang">\n${tabs.join('\n')}\n</Tabs>`
}

function metaRow(uc, lang) {
  const t = T[lang]
  const parts = [
    `**${t['uc.models']}:** ${uc.meta.models.map((m) => `\`${m}\``).join(', ')}`,
    `**${t['uc.account']}:** ${t[`tier.${uc.meta.tier}`]}`,
    `**${t['uc.languages']}:** ${uc.languages.map(langName).join(', ')}`,
  ]
  if (uc.meta.playground) parts.push(`[${t['uc.playground']}](${playgroundUrl(uc.meta.playground)})`)
  parts.push(`[${t['uc.sourceDir']}](${ghLink(uc, '')})`)
  return parts.join(' · ')
}

// ---- README -> MDX ------------------------------------------------------------------------------------------------
const copyAsset = (uc, rel) => {
  const src = join(uc.dir, rel)
  if (!existsSync(src) || !statSync(src).isFile()) return null
  const dst = join(OUT_PUBLIC, uc.id, rel)
  mkdirSync(dirname(dst), { recursive: true })
  copyFileSync(src, dst)
  return `${BASE}/usecases/${uc.id}/${rel}`
}

const INLINE_CODE = /(``[^\n]*?``|`[^`\n]+`)/

/** Text outside code made safe for MDX: relative links to the repository, no HTML comments, no stray braces or angle brackets. */
function plain(segment, uc, lang) {
  return localizeDocsLinks(segment, lang)
    .replace(/<(https?:\/\/[^>\s]+)>/g, '[$1]($1)')
    .replace(/<!--[\s\S]*?-->/g, '')
    .replace(/\]\(((?!https?:|mailto:|#|\/)[^)\s]+)\)/g, (_, rel) => `](${ghLink(uc, rel)})`)
    .replace(/[{}]/g, (c) => `\\${c}`)
    .replace(/<(?!\/?(?:br|kbd|sub|sup|details|summary|em|strong|code|a|img|hr|span)\b)/g, '&lt;')
}

function mdxSafe(text, uc, lang) {
  let open = null
  return text
    .split('\n')
    .map((line) => {
      const m = /^\s*(`{3,}|~{3,})/.exec(line)
      if (open) {
        if (m && m[1][0] === open[0] && m[1].length >= open.length && line.trim() === m[1]) open = null
        return line
      }
      if (m) {
        open = m[1]
        return line
      }
      // images first: copy the file next to the site and point to it
      line = line.replace(/!\[([^\]]*)\]\(((?!https?:|\/)[^)\s]+)\)/g, (all, alt, rel) => {
        const url = copyAsset(uc, rel)
        return url ? `![${alt}](${url})` : ''
      })
      return line
        .split(INLINE_CODE)
        .map((part, i) => (i % 2 ? part : plain(part, uc, lang)))
        .join('')
    })
    .join('\n')
}

function pageFromReadme(uc, lang) {
  const file = join(uc.dir, lang === 'en' ? 'README.md' : 'README.zh-CN.md')
  let text = readFileSync(file, 'utf8').replace(/^<!-- en-sha: [0-9a-f]{12} -->\n/, '')
  text = text.replace(/^# .*\n/, '').replace(/^(?:English \| \[|\[English\]\().*README.*\n/m, '')
  // figures that do not exist (yet) vanish together with their image line
  text = text.replace(FIGURE, (all, rel, img) => (existsSync(join(uc.dir, rel)) ? (img ?? '') : ''))
  // marker blocks become placeholders; the generated Markdown / JSX is put back after the escaping pass
  const blocks = []
  text = text.replace(BLOCK, (all, kind, arg) => {
    const body = kind === 'code' ? codeBlock(uc, arg, lang) : kind === 'metrics' ? metricsBlock(uc, arg, lang) : kind === 'cost' ? costBlock(uc, lang) : ''
    blocks.push(body)
    return `@@BLOCK${blocks.length - 1}@@`
  })
  text = text.replace(ANCHOR, '$1 @@ANCHOR:$2@@')
  text = mdxSafe(text, uc, lang)
  text = text.replace(/ @@ANCHOR:([a-z0-9-]+)@@/g, ' \\{#$1\\}')
  text = text.replace(/@@BLOCK(\d+)@@/g, (_, i) => blocks[Number(i)])
  // lead paragraph (before the first section): the summary line of the page, followed by the meta row
  const firstSection = text.search(/^## /m)
  text = `${text.slice(0, firstSection).trim()}\n\n${metaRow(uc, lang)}\n\n${text.slice(firstSection)}`
  // the generated "Run it" tabs come first in that section; the README text stays below them
  text = text.replace(/^(## .*\\\{#run\\\}\n)/m, (h) => `${h}\n${runBlock(uc, lang)}\n\n`)
  const fm = ['---', `title: ${JSON.stringify(uc.meta.title[lang])}`, `description: ${JSON.stringify(uc.meta.summary[lang])}`, `sidebar:`, `  order: ${uc.meta.order ?? 0}`, '---']
  return `${fm.join('\n')}\nimport { Tabs, TabItem } from '@astrojs/starlight/components'\n\n${text.replace(/\n{3,}/g, '\n\n').trim()}\n`
}

function homePage(lang) {
  const t = T[lang]
  const rel = lang === 'en' ? '../../components/HomeMatrix.astro' : '../../../components/HomeMatrix.astro'
  const fm = ['---', `title: ${JSON.stringify(t['site.title'])}`, `description: ${JSON.stringify(t['site.description'])}`, 'template: splash', '---']
  return `${fm.join('\n')}\nimport HomeMatrix from '${rel}'\n\n<div class="home">\n\n${t['home.intro']}\n\n## ${t['home.matrixTitle']}\n\n${t['home.matrixIntro']}\n\n<HomeMatrix locale="${lang}" />\n\n## ${t['home.linksTitle']}\n\n- [${t['home.linkDocs']}](${docsUrl(lang)})\n- [${t['home.linkPlayground']}](${playgroundUrl()})\n- [${t['home.linkRepo']}](${REPO})\n\n</div>\n`
}

// ---- write ----------------------------------------------------------------------------------------------------------
rmSync(OUT_DOCS, { recursive: true, force: true })
rmSync(OUT_DATA, { recursive: true, force: true })
rmSync(OUT_PUBLIC, { recursive: true, force: true })
const write = (path, content) => {
  mkdirSync(dirname(path), { recursive: true })
  writeFileSync(path, content)
}

for (const uc of useCases) {
  for (const lang of ['en', 'zh']) write(join(OUT_DOCS, lang === 'zh' ? 'zh' : '', 'use-cases', `${uc.id}.mdx`), pageFromReadme(uc, lang))
}
for (const lang of ['en', 'zh']) write(join(OUT_DOCS, lang === 'zh' ? 'zh' : '', 'index.mdx'), homePage(lang))

const usedLanguages = LANG_ORDER.filter((l) => useCases.some((uc) => uc.languages.includes(l)))
write(
  join(OUT_DATA, 'usecases.json'),
  JSON.stringify(
    {
      languages: usedLanguages.map((key) => ({ key, name: langName(key) })),
      useCases: useCases.map((uc) => ({ id: uc.id, num: uc.num, title: uc.meta.title, summary: uc.meta.summary, tier: uc.meta.tier, models: uc.meta.models, languages: uc.languages })),
    },
    null,
    2,
  ) + '\n',
)
console.log(`sync-usecases: ${useCases.length} use cases (${useCases.map((u) => u.num).join(', ')}), ${usedLanguages.length} languages -> ${relative(ROOT, OUT_DOCS)}`)
