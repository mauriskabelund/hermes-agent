/**
 * backend-child.ts
 *
 * Windows-aware teardown for the desktop's managed backend child process.
 *
 * Node's `child.kill()` only signals the direct child. On Windows a backend
 * that spawned its own grandchildren (a `hermes` REPL, a pty terminal
 * session, the gateway) survives a plain SIGTERM and keeps files (e.g. the
 * venv shim) locked. So on Windows we tree-kill via `forceKillProcessTree`;
 * everywhere else a plain SIGTERM is correct and sufficient (POSIX has no
 * mandatory locks, and the backend is not spawned detached so there's no
 * process-group to negative-pid-kill).
 *
 * Extracted into its own dependency-free module (no electron import) so the
 * SIGTERM-vs-tree-kill branching can be asserted directly with a fake child
 * object and a spy `forceKillProcessTree`, instead of grepping main.ts source
 * text for the function body.
 */

export interface StopBackendChildDeps {
  /** Defaults to the real platform check; injectable for tests. */
  isWindows?: boolean
  /** Windows tree-kill implementation (real: taskkill /T /F via execFileSync). */
  forceKillProcessTree: (pid: number) => void
}

export interface KillableChild {
  pid?: number | null
  killed?: boolean
  kill: (signal: string) => void
}

/**
 * Stop a managed child process, choosing the right strategy for the platform.
 * No-ops silently if `child` is falsy, already killed, or the kill attempt
 * throws (the process may already be gone) -- mirrors the original inline
 * best-effort semantics in main.ts.
 */
export interface WaitableChild extends KillableChild {
  exitCode?: number | null
  signalCode?: string | null
  once: (event: string, listener: (...args: any[]) => void) => unknown
}

export interface WaitForBackendExitDeps extends StopBackendChildDeps {
  timeoutMs?: number
  hardKillGraceMs?: number
  onHardKill?: () => void
}

/** Wait until child exit is observed; hard-kill on timeout, then fail if still unobserved. */
export async function waitForBackendExit(
  child: WaitableChild | null | undefined,
  deps: WaitForBackendExitDeps
): Promise<void> {
  if (!child) {
    return
  }

  const hasExited =
    (child.exitCode !== null && child.exitCode !== undefined) ||
    (child.signalCode !== null && child.signalCode !== undefined)

  if (hasExited) {
    return
  }

  const timeoutMs = deps.timeoutMs ?? 5000
  const hardKillGraceMs = deps.hardKillGraceMs ?? 2000

  await new Promise<void>((resolve, reject) => {
    let settled = false
    let hardKillTimer: ReturnType<typeof setTimeout> | null = null

    const settle = (error?: Error) => {
      if (settled) {
        return
      }

      settled = true
      clearTimeout(softTimer)

      if (hardKillTimer) {
        clearTimeout(hardKillTimer)
      }

      if (error) {
        reject(error)
      } else {
        resolve()
      }
    }

    const softTimer = setTimeout(() => {
      try {
        if ((deps.isWindows ?? process.platform === 'win32') && Number.isInteger(child.pid)) {
          deps.forceKillProcessTree(child.pid as number)
        } else {
          child.kill('SIGKILL')
        }
      } catch {
        // The process may already be exiting; the exit event remains authoritative.
      }

      deps.onHardKill?.()
      hardKillTimer = setTimeout(
        () => settle(new Error('Backend child did not exit after hard kill')),
        hardKillGraceMs
      )
    }, timeoutMs)

    try {
      child.once('exit', () => settle())
    } catch (error) {
      settle(error instanceof Error ? error : new Error(String(error)))
    }
  })
}

export function stopBackendChild(child: KillableChild | null | undefined, deps: StopBackendChildDeps) {
  if (!child || child.killed) {
    return
  }

  const isWindows = deps.isWindows ?? process.platform === 'win32'

  try {
    if (isWindows && Number.isInteger(child.pid)) {
      deps.forceKillProcessTree(child.pid as number)
    } else {
      child.kill('SIGTERM')
    }
  } catch {
    // Already gone.
  }
}
