// Search and link-preview metadata of every page, added to Starlight's <head> (Starlight route middleware):
//  - <title> from the page's `seoTitle` ("<seoTitle> | Stack Cookbook"; the home page uses its own title without the suffix)
//    and og:title / twitter:title to match,
//  - og:image and twitter:image (one default image per language, public/og/), with width, height and alt text,
//  - hreflang="zh" next to the zh-CN link, so Chinese readers in other regions also land on the Chinese page,
//  - JSON-LD: TechArticle (use case pages) or CollectionPage (home page), and BreadcrumbList; both as application/ld+json
//    data blocks, which scripts/csp.mjs leaves alone (they do not run, so the CSP needs no hash for them).
import { defineRouteMiddleware } from '@astrojs/starlight/route-data'
import { BRAND } from './lib/brand.ts'
import { T } from './i18n.ts'
import { BASE, SITE, counterpart, lpath, localeOf, type Locale } from './lib/site.ts'

const ORG_ID = 'https://foldbun.com/#organization'
const SUFFIX = `${BRAND.suffix} Cookbook`
const abs = (path: string): string => new URL(path, `${SITE}/`).href
const ldJson = (doc: unknown): string => JSON.stringify(doc).replace(/</g, '\\u003c')

type Head = ReturnType<typeof getHead>
const getHead = (route: { head: unknown[] }) => route.head as { tag: string; attrs?: Record<string, string | boolean | undefined>; content?: string }[]

export const onRequest = defineRouteMiddleware((context) => {
  const route = context.locals.starlightRoute
  const { entry, head } = route
  const data = entry.data as { title: string; seoTitle?: string; description?: string; lastmod?: string }
  const pathname = new URL(context.url).pathname
  const locale: Locale = localeOf(pathname)
  const t = T[locale]
  const isHome = pathname === lpath(locale, '/')
  const seoTitle = data.seoTitle ?? data.title
  const pageUrl = abs(pathname)
  const lang = locale === 'zh' ? 'zh-CN' : 'en-US'
  const image = abs(`${BASE}/og/cookbook.${lang}.png`)

  const set = (match: (h: Head[number]) => boolean, tag: Head[number]) => {
    const i = (head as Head).findIndex(match)
    if (i >= 0) head[i] = tag as never
    else head.push(tag as never)
  }
  const meta = (key: 'name' | 'property', name: string, content: string) =>
    set((h) => h.tag === 'meta' && h.attrs?.[key] === name, { tag: 'meta', attrs: { [key]: name, content } })

  // title
  set((h) => h.tag === 'title', { tag: 'title', content: isHome ? seoTitle : `${seoTitle} | ${SUFFIX}` })
  meta('property', 'og:title', seoTitle)
  meta('name', 'twitter:title', seoTitle)
  if (data.description) meta('name', 'twitter:description', data.description)

  // images
  meta('property', 'og:image', image)
  meta('property', 'og:image:type', 'image/png')
  meta('property', 'og:image:width', '1200')
  meta('property', 'og:image:height', '630')
  meta('property', 'og:image:alt', t['site.ogAlt'])
  meta('name', 'twitter:image', image)
  meta('name', 'twitter:image:alt', t['site.ogAlt'])

  // hreflang zh next to zh-CN
  const zhUrl = abs(counterpart(pathname, 'zh'))
  if (!(head as Head).some((h) => h.tag === 'link' && h.attrs?.hreflang === 'zh')) {
    head.push({ tag: 'link', attrs: { rel: 'alternate', hreflang: 'zh', href: zhUrl } } as never)
  }

  // JSON-LD
  const org = { '@type': 'Organization', '@id': ORG_ID }
  const homeUrl = abs(lpath(locale, '/'))
  const crumbs = [
    { name: BRAND.name, url: locale === 'zh' ? `${SITE}/zh/` : `${SITE}/` },
    { name: 'Cookbook', url: homeUrl },
    ...(isHome ? [] : [{ name: seoTitle, url: pageUrl }]),
  ]
  const breadcrumb = {
    '@type': 'BreadcrumbList',
    itemListElement: crumbs.map((c, i) => ({ '@type': 'ListItem', position: i + 1, name: c.name, item: c.url })),
  }
  const page = isHome
    ? { '@type': 'CollectionPage', name: seoTitle, url: pageUrl, description: data.description, inLanguage: lang, image, publisher: org, ...(data.lastmod && { dateModified: data.lastmod }) }
    : {
        '@type': 'TechArticle',
        headline: data.title,
        url: pageUrl,
        mainEntityOfPage: pageUrl,
        description: data.description,
        inLanguage: lang,
        image,
        author: org,
        publisher: org,
        isPartOf: { '@type': 'CollectionPage', name: `${BRAND.name} Cookbook`, url: homeUrl },
        ...(data.lastmod && { dateModified: data.lastmod }),
      }
  head.push({ tag: 'script', attrs: { type: 'application/ld+json' }, content: ldJson({ '@context': 'https://schema.org', '@graph': [page, breadcrumb] }) } as never)
})
