import { notifyError } from './notifications'
import { $activeGatewayProfile, normalizeProfileKey } from './profile'

// Window flag set by the Electron main process when it opens a standalone
// session window (see electron/main.ts buildSessionWindowUrl). It rides in the
// query string BEFORE the HashRouter '#', so we read it from location.search,
// never from the router. A "secondary" window renders a single chat without the
// global session sidebar or the install / onboarding overlays.
const SECONDARY_WINDOW_FLAG = 'secondary'
const PROFILE_NAME_RE = /^[a-z0-9][a-z0-9_-]{0,63}$/

let secondaryWindowCache: boolean | null = null

export function isSecondaryWindow(): boolean {
  if (secondaryWindowCache !== null) {
    return secondaryWindowCache
  }

  let result = false

  try {
    result = new URLSearchParams(window.location.search).get('win') === SECONDARY_WINDOW_FLAG
  } catch {
    result = false
  }

  secondaryWindowCache = result

  return result
}

let watchWindowCache: boolean | null = null

// A "watch" window spectates a session that is being driven elsewhere (a
// running subagent). It resumes lazily — the gateway registers history + a
// transport for the live mirror without building an agent, so opening it is
// cheap even while the backend is busy running the delegation.
export function isWatchWindow(): boolean {
  if (watchWindowCache !== null) {
    return watchWindowCache
  }

  let result = false

  try {
    result = new URLSearchParams(window.location.search).get('watch') === '1'
  } catch {
    result = false
  }

  watchWindowCache = result

  return result
}

export function initialWindowProfile(search = window.location.search): string | null {
  try {
    const value = new URLSearchParams(search).get('profile')?.trim() || ''

    return value && PROFILE_NAME_RE.test(value) ? value : null
  } catch {
    return null
  }
}

// True when running inside the Electron desktop shell (the preload bridge is
// present). The "open in new window" affordance is desktop-only.
export function canOpenSessionWindow(): boolean {
  return typeof window !== 'undefined' && typeof window.hermesDesktop?.openSessionWindow === 'function'
}

// True when the shell can open a full peer app window (⌘⇧N / "New Window").
export function canOpenNewWindow(): boolean {
  return typeof window !== 'undefined' && typeof window.hermesDesktop?.openWindow === 'function'
}

type WindowOpenResult = { ok: boolean; error?: string } | undefined

// Run a window-open bridge call, surfacing any failure as a toast. Shared by the
// session pop-out and the new-window opener.
async function runWindowOpen(call: () => Promise<WindowOpenResult>, failMessage: string): Promise<void> {
  try {
    const result = await call()

    if (!result?.ok) {
      notifyError(new Error(result?.error || 'unknown error'), failMessage)
    }
  } catch (err) {
    notifyError(err, failMessage)
  }
}

// Open (or focus) a standalone OS window for a single chat session. No-ops
// gracefully outside Electron so callers can wire it unconditionally.
// `watch: true` opens a spectator window (lazy resume, live-mirror stream).
export async function openSessionInNewWindow(sessionId: string, opts?: { watch?: boolean }): Promise<void> {
  if (!sessionId || !canOpenSessionWindow()) {
    return
  }

  await runWindowOpen(
    () => window.hermesDesktop.openSessionWindow(sessionId, opts),
    'Could not open chat in a new window'
  )
}

// Open a new full-chrome app window — a peer instance of the primary that
// renders the complete app against the shared backend. No-ops outside Electron.
export async function openNewWindow(): Promise<void> {
  if (!canOpenNewWindow()) {
    return
  }

  const profile = normalizeProfileKey($activeGatewayProfile.get())
  await runWindowOpen(() => window.hermesDesktop.openWindow(profile), 'Could not open a new window')
}
