# Theme provenance

The look of this site is the look of the Foldbun Stack documentation site (stack.foldbun.com/docs/). The theme files
below were copied from **the Foldbun Stack documentation site, version 1.2.0**. They are kept as close to the originals
as the Cookbook allows; the changes are listed per file. When the Stack documentation theme changes, copy the new
version of these files and record the version here.

| File here | What it is | Changes from the original |
|---|---|---|
| `src/styles/tokens.css` | design tokens (colours, type, spacing, radius, light and dark) | header comment shortened |
| `src/styles/components.css` | shared components (buttons, menus, badges, tables) | none |
| `src/styles/chrome.css` | top bar, footer base row, layout tokens | header comment shortened (the unused language suggestion bar rules stay) |
| `src/styles/theme.css` | maps the tokens to Starlight's variables; flat sidebar, code blocks, tables | header comment shortened (rules for parts the Cookbook does not use stay) |
| `src/components/starlight/Header.astro`, `Footer.astro`, `ThemeProvider.astro`, `Empty.astro` | Starlight component overrides | simplified: no model pages, no pricing or Console links, no language suggestion bar, no raw Markdown links |
| `src/components/SiteHeader.astro`, `SiteFooter.astro` | the top bar and the compact footer | navigation and footer links reduced to what the Cookbook needs |
| `src/components/Logo.astro`, `src/assets/brand/*.svg` | the Foldbun Stack mark, one file per theme | `Logo.astro`: import paths of the SVG files and the header comment; the SVG files are unchanged |
| `src/components/LangPref.astro`, `src/scripts/lang-pref.js` | the shared language preference (cookie `foldbun_lang`, values `en` and `zh`, written only on an explicit choice) | the migration from the Stack site's own storage keys is limited to one key |
| `src/scripts/theme-init.js`, `src/scripts/site.js` | theme menu and language links | the theme choice is stored under `foldbun.theme`; the matrix filter is new |
| `scripts/csp.mjs`, `csp.txt` | post-build step that makes the output compatible with a strict Content-Security-Policy, and the resulting policy | links the moved scripts and styles under the base path `/cookbook/` |
| `src/icons/` | the icon subset the site uses (Foldbun Icons 0.1.1, based on lucide-static 1.62.0, ISC licence, see `LICENSE-lucide.txt`) | only 9 of the 144 icons |
| `public/favicon.svg`, `favicon.ico`, `apple-touch-icon.png` | favicons | none |

Written for the Cookbook (not copied): `src/i18n.ts`, `src/lib/*`, `src/components/HomeMatrix.astro`,
`src/styles/site.css`, `src/plugins/heading-anchors.mjs`, `scripts/sync-usecases.mjs`, `scripts/check-links.mjs`.

## Trademarks

Foldbun, Foldbun Stack and the Foldbun Stack mark (the logo files under `src/assets/brand/` and the favicons) are
trademarks of Foldbun. The MIT licence of this repository covers the code, not these names and marks. Forks and copies of
this site must replace the name, the logo and the favicons with their own.
