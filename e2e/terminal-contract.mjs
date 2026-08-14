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
const contractName = `contract-check-${Date.now().toString(36)}`
let terminalId = ''
page.on('pageerror', (error) => pageErrors.push(error.message))
page.on('console', (message) => {
  if (message.type() === 'error') consoleErrors.push(message.text())
})

try {
  await page.goto(base, { waitUntil: 'networkidle' })
  await page.getByRole('button', { name: '进入控制台' }).waitFor()
  await page.getByLabel('密码').fill(password)
  await page.getByRole('button', { name: '进入控制台' }).click()
  await page.getByRole('navigation', { name: '主导航' }).waitFor()

  terminalId = await page.evaluate(async (name) => {
    const response = await fetch('/api/terminals', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name }),
    })
    if (!response.ok) throw new Error(`failed to create terminal: ${response.status}`)
    const session = await response.json()
    if (!session.id) throw new Error('created terminal has no id')
    if (session.workspace?.available !== true) {
      throw new Error(`created terminal has no workspace: ${session.workspace?.error || 'unknown error'}`)
    }
    return session.id
  }, contractName)

  await page.evaluate(async (sessionId) => {
    const request = async (path, init) => {
      const response = await fetch(path, init)
      if (!response.ok) {
        const detail = await response.text()
        throw new Error(`${init?.method || 'GET'} ${path} failed: ${response.status} ${detail}`)
      }
      return response
    }

    const workspaceResponse = await request(
      `/api/terminals/${encodeURIComponent(sessionId)}/workspace?path=`,
    )
    const workspace = await workspaceResponse.json()
    if (!Array.isArray(workspace.entries)) throw new Error('workspace entries are not an array')
    const readme = workspace.entries.find((entry) => entry.path === 'README.md')
    if (!readme || readme.kind !== 'file') throw new Error('README.md missing from terminal workspace')

    const resolveResponse = await request(
      `/api/terminals/${encodeURIComponent(sessionId)}/files/resolve`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: 'README.md' }),
      },
    )
    const grant = await resolveResponse.json()
    if (!grant.id || grant.terminal_id !== sessionId) throw new Error('invalid file grant payload')
    if (grant.path !== 'README.md' || !grant.etag) throw new Error('file grant lost path or version')

    const contentResponse = await request(
      `/api/files/${encodeURIComponent(grant.id)}/content?terminal_id=${encodeURIComponent(sessionId)}`,
      { headers: { Range: 'bytes=0-15' } },
    )
    if (contentResponse.status !== 206) {
      throw new Error(`range read returned ${contentResponse.status}, expected 206`)
    }
    if (!contentResponse.headers.get('content-range')?.startsWith('bytes 0-15/')) {
      throw new Error('range read has no valid Content-Range header')
    }
    if (contentResponse.headers.get('x-content-type-options') !== 'nosniff') {
      throw new Error('range read has no nosniff protection')
    }
    const prefix = await contentResponse.text()
    if (!prefix.startsWith('# AgentServer')) throw new Error('range read returned unexpected bytes')

    const createArtifactResponse = await request(
      `/api/terminals/${encodeURIComponent(sessionId)}/artifacts`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          type: 'contract-check',
          path: 'README.md',
          name: 'README.md',
          media_type: grant.media_type,
          size: grant.size,
          version: grant.version,
          source: 'e2e-contract',
        }),
      },
    )
    if (createArtifactResponse.status !== 201) {
      throw new Error(`artifact creation returned ${createArtifactResponse.status}, expected 201`)
    }
    const createdArtifact = await createArtifactResponse.json()
    if (!createdArtifact.id || createdArtifact.path !== 'README.md') {
      throw new Error('artifact creation returned an invalid event')
    }

    const artifactsResponse = await request(
      `/api/terminals/${encodeURIComponent(sessionId)}/artifacts`,
    )
    const artifacts = await artifactsResponse.json()
    if (!Array.isArray(artifacts)) throw new Error('artifact snapshot is not an array')
    const persisted = artifacts.find((event) => event.id === createdArtifact.id)
    if (!persisted || persisted.source !== 'e2e-contract' || persisted.schema_version !== 1) {
      throw new Error('created artifact was not present in the durable snapshot')
    }

    const readImageResponse = await request(
      `/api/terminals/${encodeURIComponent(sessionId)}/read-image`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: 'web_dist/favicon.png' }),
      },
    )
    const imageResult = await readImageResponse.json()
    if (imageResult.model_content_format !== 'openai-responses') {
      throw new Error('read-image did not identify its model content adapter format')
    }
    const imageBlock = imageResult.model_content?.find((block) => block.type === 'input_image')
    if (!imageBlock?.image_url?.startsWith('data:image/png;base64,')) {
      throw new Error('read-image did not return an inline model image block')
    }
    if (!imageResult.attachment?.id?.startsWith('sha256:')) {
      throw new Error('read-image did not return a content-addressed attachment')
    }
    const attachmentResponse = await request(imageResult.attachment.url)
    if (attachmentResponse.headers.get('content-type') !== 'image/png') {
      throw new Error('immutable attachment has an unexpected media type')
    }
    if ((await attachmentResponse.arrayBuffer()).byteLength !== imageResult.attachment.size) {
      throw new Error('immutable attachment size does not match its reference')
    }
  }, terminalId)

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

  // A real split creates a second local terminal, mounts both panes, supports
  // keyboard sash adjustment, and survives a page reload through localStorage.
  await page.getByRole('button', { name: '向右拆分终端' }).first().click()
  await page.waitForFunction(() => {
    const hosts = [...document.querySelectorAll('.terminal-host')]
    return hosts.filter((host) => {
      const rect = host.getBoundingClientRect()
      return rect.width > 0 && rect.height > 0
    }).length >= 2
  })
  const separator = page.getByRole('separator', { name: '调整左右终端宽度' }).first()
  await separator.focus()
  await page.keyboard.press('End')
  if ((await separator.getAttribute('aria-valuenow')) !== '85') {
    throw new Error('split separator End key did not select the maximum ratio')
  }
  await page.keyboard.press('Home')
  if ((await separator.getAttribute('aria-valuenow')) !== '15') {
    throw new Error('split separator Home key did not select the minimum ratio')
  }
  await page.reload({ waitUntil: 'networkidle' })
  await page.getByRole('separator', { name: '调整左右终端宽度' }).waitFor()
  await page.waitForFunction(() => {
    const hosts = [...document.querySelectorAll('.terminal-host')]
    return hosts.filter((host) => {
      const rect = host.getBoundingClientRect()
      return rect.width > 0 && rect.height > 0
    }).length >= 2
  })

  await page.getByRole('button', { name: '打开工作区文件' }).first().click()
  const workspacePane = page.getByRole('complementary', { name: /工作区/ })
  await workspacePane.waitFor()
  await workspacePane.getByRole('button', { name: /README\.md/ }).click()
  await workspacePane.locator('pre').filter({ hasText: '# AgentServer' }).waitFor()
  await workspacePane.getByRole('button', { name: '关闭文件预览' }).click()
  await workspacePane.getByRole('button', { name: /web_dist/ }).click()
  await workspacePane.getByRole('button', { name: /favicon\.png/ }).click()
  await workspacePane.getByRole('img', { name: 'favicon.png' }).waitFor()
  await workspacePane.getByRole('button', { name: /Artifacts/ }).click()
  await workspacePane.getByText('不可变').first().waitFor()
  await page.waitForTimeout(500)
  if (pageErrors.length) throw new Error(`page errors: ${pageErrors.join(' | ')}`)
  const meaningfulConsoleErrors = consoleErrors.filter(
    (item) => !item.includes('favicon') && !item.includes('401 (Unauthorized)'),
  )
  if (meaningfulConsoleErrors.length) {
    throw new Error(`console errors: ${meaningfulConsoleErrors.join(' | ')}`)
  }
  console.log('terminal split, workspace, file range, artifact, and read-image contracts passed')
} finally {
  await page.evaluate(async ({ sessionId, name }) => {
    const response = await fetch('/api/terminals')
    if (!response.ok) return
    const sessions = await response.json()
    const owned = sessions.filter((session) => session.id === sessionId || session.name === name)
    await Promise.all(owned.map((session) => (
      fetch(`/api/terminals/${encodeURIComponent(session.id)}`, { method: 'DELETE' })
    )))
  }, { sessionId: terminalId, name: contractName }).catch(() => {})
  await browser.close()
}
