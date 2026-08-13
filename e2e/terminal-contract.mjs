import { chromium } from 'playwright'

const base = process.env.E2E_BASE || 'http://127.0.0.1:18124'
const password = process.env.E2E_PASSWORD || 'e2e-test-password'
const browser = await chromium.launch({
  headless: true,
  executablePath: process.env.PLAYWRIGHT_EXECUTABLE_PATH || undefined,
})
const page = await browser.newPage({ viewport: { width: 1280, height: 800 } })
const pageErrors = []
const consoleErrors = []
page.on('pageerror', (error) => pageErrors.push(error.message))
page.on('console', (message) => {
  if (message.type() === 'error') consoleErrors.push(message.text())
})

try {
  await page.goto(base, { waitUntil: 'networkidle' })
  await page.getByLabel('密码').fill(password)
  await page.getByRole('button', { name: '进入控制台' }).click()
  await page.getByRole('navigation', { name: '主导航' }).waitFor()

  await page.evaluate(async () => {
    const response = await fetch('/api/terminals', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: 'contract-check' }),
    })
    if (!response.ok) throw new Error(`failed to create terminal: ${response.status}`)
  })

  await page.route('**/api/terminals', async (route) => {
    if (route.request().method() !== 'GET') {
      await route.continue()
      return
    }
    const response = await route.fetch()
    const sessions = await response.json()
    for (const session of sessions) delete session.services
    await route.fulfill({ response, json: sessions })
  })

  await page.goto(`${base}/terminals`, { waitUntil: 'networkidle' })
  await page.locator('.terminal-host').first().waitFor({ timeout: 10000 })
  await page.waitForTimeout(500)
  if (pageErrors.length) throw new Error(`page errors: ${pageErrors.join(' | ')}`)
  const meaningfulConsoleErrors = consoleErrors.filter(
    (item) => !item.includes('favicon') && !item.includes('401 (Unauthorized)'),
  )
  if (meaningfulConsoleErrors.length) {
    throw new Error(`console errors: ${meaningfulConsoleErrors.join(' | ')}`)
  }
  console.log('terminal contract compatibility passed')
} finally {
  await browser.close()
}
