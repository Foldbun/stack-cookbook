// Links that leave the Cookbook frame. Owner rule: every link that leaves the current layout / navigation system, or goes
// to a different app, opens in a new tab and says so (external-link icon plus visually hidden text). The Cookbook frame is
// everything under /cookbook/ (English) and /cookbook/zh/ (Chinese) of the site: those pages share one header and footer
// and stay in the same tab. Everything else is "cross app": Stack site pages (/docs/, /pricing/, /console/, /playground/),
// foldbun.com, other Foldbun hosts, GitHub and any third-party URL. mailto: and in-page anchors stay.
//
// One helper, three users: components/ExtLink.astro (template links), plugins/external-links.mjs (Markdown content) and
// scripts/external-links.test.mjs. Plain .mjs so the Node scripts and the Astro config can import it directly.
import { BRAND } from './brand.ts'
import { iconSvg } from '../icons/icon-svg.mjs'

const FRAME = '/cookbook'
const inFrame = (pathname) => pathname === FRAME || pathname.startsWith(`${FRAME}/`)

/**
 * True when `href` leaves the Cookbook frame and must open in a new tab.
 * Relative links without a leading slash resolve inside the page's own directory, so they are never cross-app.
 */
export function isCrossApp(href, siteUrl = BRAND.siteUrl) {
  if (typeof href !== 'string') return false
  const h = href.trim()
  if (h === '' || h.startsWith('#')) return false
  if (/^[a-z][a-z0-9+.-]*:/i.test(h) || h.startsWith('//')) {
    let u
    try { u = new URL(h, siteUrl) } catch { return false }
    if (u.protocol !== 'http:' && u.protocol !== 'https:') return false
    return u.host !== new URL(siteUrl).host || !inFrame(u.pathname)
  }
  if (h.startsWith('/')) return !inFrame(h.split(/[?#]/, 1)[0])
  return false
}

/** Attributes every new-tab link carries. */
export const EXT_ATTRS = { target: '_blank', rel: 'noopener' }

/** Small "opens elsewhere" icon (inline SVG, aria-hidden, no script). */
export const extIconHtml = () => iconSvg('external', { size: 12, cls: 'ext-ic' })
