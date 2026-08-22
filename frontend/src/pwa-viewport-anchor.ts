/**
 * iPadOS Web.app (home-screen PWA) regression: with a hardware keyboard
 * attached, focusing a terminal's hidden helper textarea makes the system
 * shrink and pan the visual viewport as if a soft keyboard were appearing —
 * but the keyboard never shows. The page is pushed up and a blank strip opens
 * below (same class of bug as WebKit #279904; seen on iPadOS 26).
 *
 * The app layout is pinned to the layout viewport (overflow: hidden,
 * height: 100%), so no pan is ever legitimate while a terminal is focused:
 * counter-translate #root back into the visual viewport. Chat inputs keep the
 * default behavior so a real soft keyboard on touch devices can still pan
 * them into view.
 */
export function computeViewportAnchorTransform(options: {
  standalone: boolean
  terminalFocused: boolean
  offsetLeft: number
  offsetTop: number
}): string {
  const { standalone, terminalFocused, offsetLeft, offsetTop } = options
  if (!standalone || !terminalFocused) return ''
  if (offsetLeft === 0 && offsetTop === 0) return ''
  return `translate(${-offsetLeft}px, ${-offsetTop}px)`
}

export function installPwaViewportAnchor(root: HTMLElement | null): void {
  const viewport = window.visualViewport
  if (!root || !viewport) return

  const reanchor = () => {
    const active = document.activeElement
    root.style.transform = computeViewportAnchorTransform({
      standalone:
        window.matchMedia('(display-mode: standalone)').matches ||
        (navigator as unknown as { standalone?: boolean }).standalone === true,
      terminalFocused:
        active instanceof HTMLElement && active.classList.contains('xterm-helper-textarea'),
      offsetLeft: viewport.offsetLeft,
      offsetTop: viewport.offsetTop,
    })
  }

  viewport.addEventListener('scroll', reanchor)
  viewport.addEventListener('resize', reanchor)
  document.addEventListener('focusin', reanchor)
  document.addEventListener('focusout', reanchor)
}
