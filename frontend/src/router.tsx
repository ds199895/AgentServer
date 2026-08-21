import { createBrowserRouter, Navigate, Outlet } from 'react-router-dom'
import App from './App'
import SessionsPage from './sessions/SessionsPage'

// Wrapper component that provides header navigation
function RootLayout() {
  return (
    <div className="h-screen flex flex-col">
      {/* Header with navigation tabs */}
      <header className="border-b border-border bg-card px-6 py-3 flex items-center gap-6">
        <div className="font-semibold text-lg">Agent Server</div>
        <nav className="flex gap-1">
          <a
            href="/devices"
            className="px-4 py-2 rounded-md text-sm font-medium hover:bg-accent transition-colors"
          >
            Devices
          </a>
          <a
            href="/sessions"
            className="px-4 py-2 rounded-md text-sm font-medium hover:bg-accent transition-colors"
          >
            Sessions
          </a>
          <a
            href="/settings"
            className="px-4 py-2 rounded-md text-sm font-medium hover:bg-accent transition-colors"
          >
            Settings
          </a>
        </nav>
      </header>
      <main className="flex-1 overflow-hidden">
        <Outlet />
      </main>
    </div>
  )
}

export const router = createBrowserRouter([
  {
    path: '/',
    element: <RootLayout />,
    children: [
      { index: true, element: <Navigate to="/devices" replace /> },
      { path: 'devices', element: <App /> },
      { path: 'sessions', element: <SessionsPage /> },
      { path: 'sessions/:sessionId', element: <SessionsPage /> },
      { path: 'settings', element: <div className="p-6 text-muted-foreground">Settings page</div> },
    ],
  },
])
