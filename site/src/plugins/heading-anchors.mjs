// Sätteri HAST plugin: a heading that ends in "{#anchor}" gets that id and loses the suffix. The use case READMEs carry
// shared anchors (`## Problem <!-- #problem -->`) and sync-usecases.mjs writes them as an escaped "\{#problem\}", so
// MDX does not parse them as an expression. Astro's own heading-id plugin runs after this one and keeps an id that is
// already set, so the table of contents and the links use the shared anchors.
export const headingAnchors = {
  name: 'heading-anchors',
  element: {
    filter: ['h1', 'h2', 'h3', 'h4', 'h5', 'h6'],
    visit(node, ctx) {
      const last = node.children.at(-1)
      const m = last?.type === 'text' ? /\s*\{#([a-z0-9-]+)\}\s*$/.exec(last.value) : null
      if (!m) return
      ctx.setField(last, 'value', last.value.slice(0, m.index))
      ctx.setProperty(node, 'id', m[1])
    },
  },
}
