// Last modification time of a page, from the history of its sources (never the build time).
// Returns null when the history cannot tell: no repository, a shallow clone (the log would be cut off and the time wrong), or
// no commit touching the paths. A page without a time gets no <lastmod> in the sitemap and no dateModified in its JSON-LD.
import { execFileSync } from 'node:child_process'

const run = (cwd, args) => execFileSync('git', args, { cwd, encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] }).trim()

/** ISO 8601 UTC time (second precision) of the newest commit that touched any of `paths` (relative to `cwd`), or null. */
export function lastCommitTime(cwd, paths) {
  try {
    if (run(cwd, ['rev-parse', '--is-shallow-repository']) !== 'false') return null
    const out = run(cwd, ['log', '-1', '--format=%cI', '--', ...paths])
    return out ? new Date(out).toISOString().replace(/\.\d{3}Z$/, 'Z') : null
  } catch {
    return null
  }
}
