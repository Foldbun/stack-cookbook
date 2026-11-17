import { defineCollection } from 'astro:content'
import { z } from 'astro/zod'
import { docsLoader, i18nLoader } from '@astrojs/starlight/loaders'
import { docsSchema, i18nSchema } from '@astrojs/starlight/schema'

// Extra front matter written by scripts/sync-usecases.mjs: `seoTitle` (short title for search results, see usecase.toml
// `seo_title`) and `lastmod` (time of the last commit touching the page sources; absent when git cannot tell).
export const collections = {
  docs: defineCollection({ loader: docsLoader(), schema: docsSchema({ extend: z.object({ seoTitle: z.string().optional(), lastmod: z.string().optional() }) }) }),
  i18n: defineCollection({ loader: i18nLoader(), schema: i18nSchema() }),
}
