// Brand constants of the Cookbook site (same values as the Foldbun Stack documentation site). Plain constants only,
// so the Node scripts can import this file directly.
export const BRAND = {
  org: 'Foldbun',
  name: 'Foldbun Stack',
  suffix: 'Stack',
  tagline: { en: 'pay-per-use vision model API', zh: '按量计费的视觉模型 API' },
  /** Public URLs, no trailing slash. This site is served under /cookbook/ of the Stack site. */
  siteUrl: 'https://stack.foldbun.com',
  apiUrl: 'https://api.stack.foldbun.com',
  /** Action colour (light theme); the dark theme uses the --accent token. */
  color: '#8b502d',
}
