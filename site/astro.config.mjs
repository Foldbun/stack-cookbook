import { defineConfig } from 'astro/config'
import starlight from '@astrojs/starlight'
import { BRAND } from './src/lib/brand.ts'
import { satteri } from '@astrojs/markdown-satteri'
import { headingAnchors } from './src/plugins/heading-anchors.mjs'

// Foldbun Stack Cookbook: use cases (Starlight) under /cookbook/ (en-US) and /cookbook/zh/ (zh-CN). The pages under
// src/content/docs are generated from the use case sources by scripts/sync-usecases.mjs (pnpm build runs it first).
// Output is static (dist/). No inline script or style may reach the output: see scripts/csp.mjs and csp.txt.
export default defineConfig({
  site: BRAND.siteUrl,
  base: '/cookbook/',
  trailingSlash: 'always',
  prefetch: false,
  build: { format: 'directory', inlineStylesheets: 'never' },
  vite: { build: { assetsInlineLimit: 0 } },
  markdown: { processor: satteri({ hastPlugins: [headingAnchors] }) },
  integrations: [
    starlight({
      title: { 'en-US': `${BRAND.name} Cookbook`, 'zh-CN': `${BRAND.name} Cookbook` },
      description: `${BRAND.name} — ${BRAND.tagline.en}. Runnable use cases.`,
      logo: { light: './src/assets/brand/foldbun-stack-color-wheat-light.svg', dark: './src/assets/brand/foldbun-stack-color-wheat-dark.svg', alt: BRAND.name },
      favicon: '/favicon.svg',
      head: [
        { tag: 'link', attrs: { rel: 'icon', href: '/cookbook/favicon.ico', sizes: '32x32' } },
        { tag: 'link', attrs: { rel: 'apple-touch-icon', href: '/cookbook/apple-touch-icon.png' } },
        { tag: 'meta', attrs: { name: 'theme-color', content: '#ffffff', media: '(prefers-color-scheme: light)' } },
        { tag: 'meta', attrs: { name: 'theme-color', content: '#222722', media: '(prefers-color-scheme: dark)' } },
        { tag: 'meta', attrs: { property: 'og:site_name', content: `${BRAND.name} Cookbook` } },
      ],
      defaultLocale: 'root',
      locales: {
        root: { label: 'English', lang: 'en-US' },
        zh: { label: '简体中文', lang: 'zh-CN' },
      },
      // Flat code blocks: no terminal window chrome, 2px radius, mono font from the tokens.
      expressiveCode: {
        defaultProps: { frame: 'code' },
        styleOverrides: {
          borderRadius: '2px',
          borderColor: 'var(--border)',
          codeFontFamily: 'var(--mono)',
          uiFontFamily: 'var(--font)',
          codeFontSize: '0.8125rem',
          frames: { shadowColor: 'transparent' },
        },
      },
      customCss: ['./src/styles/tokens.css', './src/styles/components.css', './src/icons/icons.css', './src/styles/chrome.css', './src/styles/theme.css', './src/styles/site.css'],
      components: {
        Header: './src/components/starlight/Header.astro',
        ThemeProvider: './src/components/starlight/ThemeProvider.astro',
        ThemeSelect: './src/components/starlight/Empty.astro',
        LanguageSelect: './src/components/starlight/Empty.astro',
        SocialIcons: './src/components/starlight/Empty.astro',
        Footer: './src/components/starlight/Footer.astro',
      },
      lastUpdated: false,
      pagination: true,
      sidebar: [{ label: 'Use cases', translations: { 'zh-CN': '用例' }, items: [{ autogenerate: { directory: 'use-cases' } }] }],
    }),
  ],
})
