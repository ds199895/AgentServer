/**
 * TEMPORARY diagnostics for the iPadOS Web.app phantom-viewport bug: with a
 * hardware keyboard attached, focusing a terminal pane shoves the page up and
 * leaves a blank strip below. The counter-translate in pwa-viewport-anchor
 * did not fix it, so we need ground truth from the device: this overlay shows
 * live visualViewport / scroll metrics in standalone mode. Remove once the
 * real fix lands.
 */
export function installPwaViewportDebug(): void {
  const standalone =
    window.matchMedia('(display-mode: standalone)').matches ||
    (navigator as unknown as { standalone?: boolean }).standalone === true
  if (!standalone) return

  const el = document.createElement('div')
  el.style.cssText =
    'position:fixed;top:0;left:0;z-index:99999;background:#000c;color:#7f7;' +
    'font:11px/1.4 monospace;padding:2px 6px;pointer-events:none;white-space:pre'
  document.body.appendChild(el)

  const update = () => {
    const vv = window.visualViewport
    const active = document.activeElement
    const activeLabel =
      active instanceof HTMLElement ? active.className || active.tagName : (active?.tagName ?? '-')
    el.textContent = [
      `vv off=${vv?.offsetLeft.toFixed(0)},${vv?.offsetTop.toFixed(0)} size=${vv?.width.toFixed(0)}x${vv?.height.toFixed(0)} scale=${vv?.scale.toFixed(2)}`,
      `inner=${window.innerWidth}x${window.innerHeight} scroll=${window.scrollX.toFixed(0)},${window.scrollY.toFixed(0)} bodyScroll=${document.body.scrollTop}`,
      `rootTransform=${document.getElementById('root')?.style.transform || '-'} focus=${activeLabel.slice(0, 40)}`,
    ].join('\n')
  }

  window.visualViewport?.addEventListener('scroll', update)
  window.visualViewport?.addEventListener('resize', update)
  window.addEventListener('scroll', update)
  setInterval(update, 500)
  update()
}
