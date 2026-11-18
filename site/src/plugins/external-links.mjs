// Sätteri HAST plugin: every link in Markdown content that leaves the Cookbook frame (see src/lib/external.mjs) gets
// target="_blank" rel="noopener", an external-link icon and visually hidden "(opens in a new tab)" text in the page language.
// The use case pages are generated from the READMEs, so content links are handled here and not in the READMEs.
import { fileURLToPath } from 'node:url'
import { isCrossApp, EXT_ATTRS, extIconHtml } from '../lib/external.mjs'
import { T } from '../i18n.ts'

// The Chinese pages live in src/content/docs/zh/, the English ones in the root of that directory.
const localeOf = (ctx) => {
  try { return ctx.fileURL && /[\\/]docs[\\/]zh[\\/]/.test(fileURLToPath(ctx.fileURL)) ? 'zh' : 'en' } catch { return 'en' }
}

export const externalLinks = {
  name: 'external-links',
  element: {
    filter: ['a'],
    visit(node, ctx) {
      const href = node.properties?.href
      if (!isCrossApp(typeof href === 'string' ? href : undefined)) return
      ctx.setProperty(node, 'target', EXT_ATTRS.target)
      ctx.setProperty(node, 'rel', EXT_ATTRS.rel)
      const text = T[localeOf(ctx)]['link.newTab']
      ctx.appendChild(node, [
        { type: 'raw', value: extIconHtml() },
        { type: 'element', tagName: 'span', properties: { className: ['vh'] }, children: [{ type: 'text', value: ` ${text}` }] },
      ])
    },
  },
}
