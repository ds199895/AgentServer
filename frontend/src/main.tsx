import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import '@xterm/xterm/css/xterm.css'
import './index.css'
import App from './App'
import { installPwaViewportAnchor } from './pwa-viewport-anchor'
import { installPwaViewportDebug } from './pwa-viewport-debug'

installPwaViewportAnchor(document.getElementById('root'))
installPwaViewportDebug()

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
