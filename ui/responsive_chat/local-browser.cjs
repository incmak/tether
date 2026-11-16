// Run existing fixture suites on the spec-required local browser, without changing shared helpers.
const path = require('node:path')
const helper = require(path.resolve(__dirname, '../browser_test_helper.cjs'))
const original = helper.getBrowser
helper.getBrowser = () => original({ local: true })

delete process.env.TETHER_CDP_URL
