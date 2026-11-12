# Cookbook site

The documentation site of the Stack Cookbook: Astro + Starlight, English at the root and Chinese under `/zh/`, built to
be served at `https://stack.foldbun.com/cookbook/` (`base: '/cookbook/'`). The look is that of the Foldbun Stack
documentation site; `THEME.md` lists which files were copied from it.

## Build and preview

Node 24 and pnpm are needed (nothing else; Python and the use case toolchains are not involved).

```bash
pnpm --dir site install --frozen-lockfile
pnpm --dir site build          # generate the pages, build, make the output CSP-clean, check the links
pnpm --dir site preview --host 127.0.0.1 --port 4321   # then open http://127.0.0.1:4321/cookbook/
```

`pnpm --dir site dev` regenerates the pages once and starts the development server. After changing a README or a source
file while it runs, run `pnpm --dir site sync` to regenerate the pages.

`pnpm build` runs four steps: `scripts/sync-usecases.mjs` (generate), `astro build`, `scripts/csp.mjs` (move inline
scripts and styles into files, write `csp.txt`, the policy the web server sends) and `scripts/check-links.mjs` (every
internal link, script, stylesheet and image of every built page resolves under `/cookbook/`; every `#fragment` names an
id of the target page).

## What is generated

Nothing under `src/content/docs/`, `src/generated/` or `public/usecases/` is committed; all three are rebuilt from the
repository by `scripts/sync-usecases.mjs`, which reads the use cases it finds (it lists neither use cases nor languages):

- **One English and one Chinese page per use case** from `README.md` and `README.zh-CN.md`, with the same section anchors
  (`## Problem <!-- #problem -->` becomes the id `problem`).
- **Code** is read from the source files by the region markers, with the rules of `harness/snippets.py`, never from the
  copies the harness expands into the READMEs. Each `<!-- code: name -->` block becomes `<Tabs syncKey="lang">` with one
  tab for every language the use case has; a region missing in a language fails the build.
- **Run it**: tabs with the setup and run commands of each language, from `usecase.toml` and `harness/languages.toml`,
  placed above the README text of that section.
- **Metrics and cost** from `expected/*.json`, **figures** from the files the READMEs reference (copied to
  `public/usecases/`). A block whose file does not exist yet is left out.
- Relative links in a README become GitHub links; links to the Stack documentation point to `/zh/docs/` on Chinese pages.
- **The home pages** (`index.mdx`, `zh/index.mdx`) from the strings of `src/i18n.ts`, and `src/generated/usecases.json`,
  the data of the use case x language matrix (`src/components/HomeMatrix.astro`). The language filter of the matrix is
  progressive: without JavaScript all rows show.

## Fixed strings

Navigation, footer and home page texts are in `src/i18n.ts`: flat `'key': value,` entries in an `en` and a `zh` block.
`uv run python -m harness i18n` (part of `harness check`) fails when the two key sets differ. Chinese copy is formal, has
no personal pronouns and no exclamation marks; product names and "Playground" stay in English.

## Language preference

The language links of the header and footer write the cookie `foldbun_lang` (`en` or `zh`, one year, `Domain=foldbun.com`
on Foldbun hosts) when clicked, never on a plain visit. `src/scripts/lang-pref.js` runs before first paint and, if the
cookie names the other language, replaces the location with the same page in that language.
