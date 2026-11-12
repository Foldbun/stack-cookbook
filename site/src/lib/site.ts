// URL helpers and outbound links of the Cookbook site. Page paths carry the base ("/cookbook/").
import { BRAND } from './brand.ts'

export type Locale = 'en' | 'zh'
export const LOCALES: Locale[] = ['en', 'zh']
export const BASE = '/cookbook'
export const REPO = 'https://github.com/Foldbun/stack-cookbook'
export const SITE = BRAND.siteUrl

/** Link targets on the Stack site (a separate site on the same host; the Cookbook only links to them). */
export const docsUrl = (l: Locale, path = ''): string => `${SITE}${l === 'zh' ? '/zh' : ''}/docs/${path}`
export const playgroundUrl = (path = '/playground/'): string => `${SITE}${path}`
/** Documentation links in use case texts are written for the English site; the Chinese pages point to the /zh/ variant. */
export const localizeDocsLinks = (text: string, l: Locale): string => (l === 'zh' ? text.replaceAll(`${SITE}/docs/`, `${SITE}/zh/docs/`) : text)

/** Path of a locale-independent page path ('/', '/use-cases/x/') in a locale, with the base. */
export const lpath = (l: Locale, p: string): string => `${BASE}${l === 'zh' ? '/zh' : ''}${p}`
export const localeOf = (pathname: string): Locale => (new RegExp(`^${BASE}/zh(/|$)`).test(pathname) ? 'zh' : 'en')
/** The page in the other locale (every page exists in both; the use case pages share their slugs). */
export const is404 = (pathname: string): boolean => /\/404(\.html|\/)?$/.test(pathname)
export const counterpart = (pathname: string, to: Locale): string => {
  const rest = pathname.replace(new RegExp(`^${BASE}(/zh)?`), '')
  return lpath(to, rest && !is404(pathname) ? rest : '/')
}
