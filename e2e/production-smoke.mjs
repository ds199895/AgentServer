import { chromium } from 'playwright'

const base = process.env.AGENTSERVER_BASE
const password = process.env.AGENTSERVER_PASSWORD
if (!base || !password) throw new Error('AGENTSERVER_BASE and AGENTSERVER_PASSWORD are required')

const browser = await chromium.launch({
  headless: true,
  executablePath: process.env.PLAYWRIGHT_EXECUTABLE_PATH || undefined,
})
const page = await browser.newPage({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true })
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
  await page.getByRole('navigation', { name: '主导航' }).waitFor({ timeout: 10000 })
  await page.goto(`${base.replace(/\/$/, '')}/terminals`, { waitUntil: 'networkidle' })
  const terminalCount = await page.locator('.terminal-host').count()
  if (terminalCount > 0) await page.locator('.terminal-host').first().waitFor({ timeout: 10000 })
  if (pageErrors.length) throw new Error(`page errors: ${pageErrors.join(' | ')}`)
  const meaningfulConsoleErrors = consoleErrors.filter(
    (item) => !item.includes('favicon') && !item.includes('401 (Unauthorized)'),
  )
  if (meaningfulConsoleErrors.length) {
    throw new Error(`console errors: ${meaningfulConsoleErrors.join(' | ')}`)
  }
  console.log(`production browser smoke passed (${terminalCount} terminal panes)`)
} finally {
  await browser.close()
}
