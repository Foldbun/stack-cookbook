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

/** Legal pages and the contact page. The text of record lives on the organisation site (foldbun.com); every Foldbun product links to it. */
export const LEGAL_URLS = {
  terms: { en: 'https://foldbun.com/terms/', zh: 'https://foldbun.com/zh/terms/' },
  privacy: { en: 'https://foldbun.com/privacy/', zh: 'https://foldbun.com/zh/privacy/' },
  acceptableUse: { en: 'https://foldbun.com/acceptable-use/', zh: 'https://foldbun.com/zh/acceptable-use/' },
  refunds: { en: 'https://foldbun.com/refunds/', zh: 'https://foldbun.com/zh/refunds/' },
  contact: { en: 'https://foldbun.com/contact/', zh: 'https://foldbun.com/zh/contact/' },
}
