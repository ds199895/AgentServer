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
const terminalDeleteRequests = []
const contractName = `contract-check-${Date.now().toString(36)}`
const switchName = `${contractName}-switch`
const scalePrefix = `${contractName}-scale-`
let terminalId = ''
let switchTerminalId = ''
page.on('pageerror', (error) => pageErrors.push(error.message))
page.on('console', (message) => {
  if (message.type() === 'error') consoleErrors.push(message.text())
})
page.on('request', (request) => {
  if (request.method() === 'DELETE' && request.url().includes('/api/terminals/')) {
    terminalDeleteRequests.push(request.url())
  }
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

  switchTerminalId = await page.evaluate(async (name) => {
    const response = await fetch('/api/terminals', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name }),
    })
    if (!response.ok) throw new Error(`failed to create switch target: ${response.status}`)
    const session = await response.json()
    if (!session.id) throw new Error('switch target has no id')
    return session.id
  }, switchName)

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
  await page.getByRole('button', { name: '新建同设备终端并向右分屏' }).first().click()
  await page.waitForFunction(() => {
    const hosts = [...document.querySelectorAll('.terminal-host')]
    return hosts.filter((host) => {
      const rect = host.getBoundingClientRect()
      return rect.width > 0 && rect.height > 0
    }).length >= 2
  })

  // Every entry point uses the same reveal rule. A hidden terminal remains in
  // its owning pane and focuses that pane; selecting it must never implicitly
  // move it into whichever pane happened to be focused last.
  const primaryPane = page.locator(`[data-terminal-id="${terminalId}"]`)
  const visiblePanes = page.locator('[data-terminal-visible="true"]')
  const initialVisible = await visiblePanes.evaluateAll((nodes) => nodes.map((node) => ({
    id: node.getAttribute('data-terminal-id'),
    left: node.getBoundingClientRect().left,
  })).sort((first, second) => first.left - second.left))
  if (initialVisible.length !== 2 || initialVisible[0].id !== terminalId) {
    throw new Error(`unexpected initial split placement: ${JSON.stringify(initialVisible)}`)
  }
  const rightTerminalId = initialVisible[1].id
  const rightPane = page.locator(`[data-terminal-id="${rightTerminalId}"]`)
  const rightLeafId = await rightPane.getAttribute('data-terminal-leaf-id')
  await primaryPane.locator('.terminal-host').click({ position: { x: 80, y: 80 } })
  await page.waitForFunction((id) => (
    document.querySelector(`[data-terminal-id="${id}"]`)?.getAttribute('data-terminal-focused') === 'true'
  ), terminalId)
  await rightPane.locator('.terminal-host').click({ position: { x: 80, y: 80 } })
  await page.waitForFunction((id) => (
    document.querySelector(`[data-terminal-id="${id}"]`)?.getAttribute('data-terminal-focused') === 'true'
  ), rightTerminalId)
  await page.getByRole('button', { name: '搜索终端' }).click()
  const terminalSearch = page.getByRole('combobox')
  await terminalSearch.fill(switchName)
  await terminalSearch.press('Enter')
  await page.waitForFunction(({ targetId, rightId }) => {
    const right = document.querySelector(`[data-terminal-id="${rightId}"]`)
    const target = document.querySelector(`[data-terminal-id="${targetId}"]`)
    if (!right || !target) return false
    const rightRect = right.getBoundingClientRect()
    const targetRect = target.getBoundingClientRect()
    return (
      right.getAttribute('data-terminal-visible') === 'true' &&
      target.getAttribute('data-terminal-visible') === 'true' &&
      target.getAttribute('data-terminal-focused') === 'true' &&
      targetRect.left < rightRect.left
    )
  }, { targetId: switchTerminalId, rightId: rightTerminalId })
  if (await rightPane.getAttribute('data-terminal-visible') !== 'true') {
    throw new Error('revealing a hidden terminal unexpectedly replaced the other pane')
  }
  const targetPane = page.locator(`[data-terminal-id="${switchTerminalId}"]`)
  if (
    !rightLeafId ||
    await targetPane.getAttribute('data-terminal-leaf-id') === rightLeafId ||
    await rightPane.getAttribute('data-terminal-leaf-id') !== rightLeafId
  ) {
    throw new Error('selected terminal was implicitly moved into the focused right pane')
  }
  await page.getByRole('button', { name: new RegExp(`^\u5207\u6362\u5230 .* ${terminalId.slice(0, 8)}$`) }).click()
  await page.waitForFunction((id) => (
    document.querySelector(`[data-terminal-id="${id}"]`)?.getAttribute('data-terminal-focused') === 'true'
  ), terminalId)
  if (
    await targetPane.getAttribute('data-terminal-visible') !== 'false' ||
    await rightPane.getAttribute('data-terminal-visible') !== 'true'
  ) {
    throw new Error('switching a pane-local tab changed the other visible pane')
  }

  // Focus mode is a view-only toggle: it temporarily shows one terminal and
  // restores the persisted split without changing pane membership.
  await page.getByRole('button', { name: '聚焦当前终端' }).click()
  await page.waitForFunction(() => (
    document.querySelectorAll('[data-terminal-visible="true"]').length === 1
  ))
  await page.getByRole('button', { name: '恢复分屏' }).click()
  await page.waitForFunction(() => (
    document.querySelectorAll('[data-terminal-visible="true"]').length === 2
  ))

  const separator = page.getByRole('separator', { name: '调整左右终端宽度' }).first()
  await separator.focus()
  await page.keyboard.press('End')
  await page.waitForFunction(() => (
    document.querySelector('[role="separator"][aria-label="调整左右终端宽度"]')?.getAttribute('aria-valuenow') === '85'
  ))
  await separator.focus()
  await page.keyboard.press('Home')
  await page.waitForFunction(() => (
    document.querySelector('[role="separator"][aria-label="调整左右终端宽度"]')?.getAttribute('aria-valuenow') === '15'
  ))
  await page.reload({ waitUntil: 'networkidle' })
  await page.getByRole('separator', { name: '调整左右终端宽度' }).waitFor()
  await page.waitForFunction(() => {
    const hosts = [...document.querySelectorAll('.terminal-host')]
    return hosts.filter((host) => {
      const rect = host.getBoundingClientRect()
      return rect.width > 0 && rect.height > 0
    }).length >= 2
  })

  // A larger session pool must remain searchable without mounting one xterm
  // and WebSocket per hidden terminal. The result list itself must scroll.
  await page.evaluate(async ({ prefix, count }) => {
    await Promise.all(Array.from({ length: count }, async (_, index) => {
      const response = await fetch('/api/terminals', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: `${prefix}${index + 1}` }),
      })
      if (!response.ok) throw new Error(`failed to create scale terminal ${index + 1}: ${response.status}`)
    }))
  }, { prefix: scalePrefix, count: 18 })
  await page.reload({ waitUntil: 'networkidle' })
  await page.getByRole('separator', { name: '调整左右终端宽度' }).waitFor()
  const mountedTerminalCount = Number(await page.locator('[data-mounted-terminal-count]').getAttribute('data-mounted-terminal-count'))
  if (!Number.isFinite(mountedTerminalCount) || mountedTerminalCount > 8) {
    throw new Error(`hidden terminal cache mounted ${mountedTerminalCount} terminals, expected at most 8`)
  }
  await page.getByRole('button', { name: '搜索终端' }).click()
  await page.getByRole('combobox').fill(scalePrefix)
  await page.waitForFunction((expected) => document.querySelectorAll('[role="option"]').length === expected, 18)
  const searchListMetrics = await page.getByRole('listbox', { name: '终端搜索结果' }).evaluate((element) => ({
    clientHeight: element.clientHeight,
    scrollHeight: element.scrollHeight,
    overflowY: getComputedStyle(element).overflowY,
  }))
  if (searchListMetrics.scrollHeight <= searchListMetrics.clientHeight || !['auto', 'scroll'].includes(searchListMetrics.overflowY)) {
    throw new Error(`terminal search results are not scrollable: ${JSON.stringify(searchListMetrics)}`)
  }
  await page.keyboard.press('Escape')

  // Visible groups expand automatically, but remain an explicit user choice.
  // Collapsing a busy group must actually hide its chips instead of immediately
  // reopening it, and the responsive search dialog must stay usable on phones.
  const localGroup = page.locator('section[aria-label^="本机，"]').first()
  const localGroupToggle = localGroup.locator('button[aria-expanded]').first()
  await localGroupToggle.click()
  if (await localGroupToggle.getAttribute('aria-expanded') !== 'false') {
    throw new Error('the visible terminal group could not be collapsed')
  }
  if (await localGroup.getByRole('button', { name: /^切换到 / }).count() !== 0) {
    throw new Error('collapsed terminal group still exposes session chips')
  }
  await localGroupToggle.click()
  await page.setViewportSize({ width: 390, height: 844 })
  await page.getByRole('button', { name: '搜索终端' }).click()
  await page.getByRole('combobox').fill(scalePrefix)
  await page.waitForFunction((expected) => document.querySelectorAll('[role="option"]').length === expected, 18)
  const mobileDialog = await page.getByRole('dialog').boundingBox()
  if (!mobileDialog || mobileDialog.x < 0 || mobileDialog.x + mobileDialog.width > 390) {
    throw new Error(`terminal search dialog overflows the mobile viewport: ${JSON.stringify(mobileDialog)}`)
  }
  const mobileSearchListMetrics = await page.getByRole('listbox', { name: '终端搜索结果' }).evaluate((element) => ({
    clientHeight: element.clientHeight,
    scrollHeight: element.scrollHeight,
    overflowY: getComputedStyle(element).overflowY,
  }))
  if (
    mobileSearchListMetrics.scrollHeight <= mobileSearchListMetrics.clientHeight ||
    !['auto', 'scroll'].includes(mobileSearchListMetrics.overflowY)
  ) {
    throw new Error(`mobile terminal search results are not scrollable: ${JSON.stringify(mobileSearchListMetrics)}`)
  }
  await page.keyboard.press('Escape')
  await page.setViewportSize({ width: 1280, height: 800 })

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

  // Closing a pane is a layout-only operation. The backend terminal remains
  // available after the split geometry collapses.
  await workspacePane.getByRole('button', { name: '关闭工作区' }).click()
  const receiverLeafId = await primaryPane.getAttribute('data-terminal-leaf-id')
  await rightPane.getByRole('button', { name: '关闭窗格（保留终端）' }).click()
  await page.waitForFunction(() => document.querySelectorAll('[role="separator"]').length === 0)
  if (
    !receiverLeafId ||
    await rightPane.getAttribute('data-terminal-leaf-id') !== receiverLeafId
  ) {
    throw new Error('closing a pane did not retain its terminal in the receiving leaf')
  }
  if (terminalDeleteRequests.some((url) => url.endsWith(`/api/terminals/${rightTerminalId}`))) {
    throw new Error('closing a pane issued a terminal DELETE request')
  }
  const retainedAfterPaneClose = await page.evaluate(async (sessionId) => {
    const response = await fetch('/api/terminals')
    if (!response.ok) return false
    const currentSessions = await response.json()
    return currentSessions.some((session) => session.id === sessionId)
  }, rightTerminalId)
  if (!retainedAfterPaneClose) throw new Error('closing a pane terminated its backend session')
  await page.getByRole('button', { name: '搜索终端' }).click()
  await page.getByRole('combobox').fill(rightTerminalId)
  await page.getByRole('combobox').press('Enter')
  await page.waitForFunction(({ sessionId, leafId }) => {
    const pane = document.querySelector(`[data-terminal-id="${sessionId}"]`)
    return (
      pane?.getAttribute('data-terminal-visible') === 'true' &&
      pane.getAttribute('data-terminal-focused') === 'true' &&
      pane.getAttribute('data-terminal-leaf-id') === leafId &&
      document.querySelectorAll('[role="separator"]').length === 0
    )
  }, { sessionId: rightTerminalId, leafId: receiverLeafId })
  await page.reload({ waitUntil: 'networkidle' })
  await page.waitForFunction((sessionId) => (
    document.querySelector(`[data-terminal-id="${sessionId}"]`)?.getAttribute('data-terminal-visible') === 'true' &&
    document.querySelectorAll('[role="separator"]').length === 0
  ), rightTerminalId)

  await page.waitForTimeout(500)
  if (pageErrors.length) throw new Error(`page errors: ${pageErrors.join(' | ')}`)
  const meaningfulConsoleErrors = consoleErrors.filter(
    (item) => !item.includes('favicon') && !item.includes('401 (Unauthorized)'),
  )
  if (meaningfulConsoleErrors.length) {
    throw new Error(`console errors: ${meaningfulConsoleErrors.join(' | ')}`)
  }
  console.log('terminal search/reveal, split focus/close, workspace, file range, artifact, and read-image contracts passed')
} finally {
  await page.evaluate(async ({ sessionId, switchSessionId, name, alternateName, scaleNamePrefix }) => {
    const response = await fetch('/api/terminals')
    if (!response.ok) return
    const sessions = await response.json()
    const owned = sessions.filter((session) => (
      session.id === sessionId ||
      session.id === switchSessionId ||
      session.name === name ||
      session.name === alternateName ||
      session.name.startsWith(scaleNamePrefix)
    ))
    await Promise.all(owned.map((session) => (
      fetch(`/api/terminals/${encodeURIComponent(session.id)}`, { method: 'DELETE' })
    )))
  }, {
    sessionId: terminalId,
    switchSessionId: switchTerminalId,
    name: contractName,
    alternateName: switchName,
    scaleNamePrefix: scalePrefix,
  }).catch(() => {})
  await browser.close()
}
