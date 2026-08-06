import crypto from 'node:crypto'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

const LOCKFILE_SCHEMA_VERSION = 2
const PROTOCOL_VERSION = 1
const DEFAULT_COMPANION_ROOT = path.join(os.homedir(), '.hermes', 'desktop-ssh')
const PROFILE_NAME_RE = /^[a-z0-9][a-z0-9_-]{0,63}$/
const LOCAL_DESKTOP_INSTANCE_ID = crypto.randomBytes(16).toString('hex')
const liveSpawnNonces = new Set<string>()

export type LocalCompanionPublication = {
  root: string
  ownershipId: string
  spawnNonce: string
  directory: string
  markerPath: string
  lockPath: string
  tokenPath: string
}

type PublishLocalCompanionOptions = {
  root?: string
  profile: string
  pid: number
  backendPid?: number
  port: number
  token: string
}

function writePrivateAtomic(target: string, contents: string) {
  const temporary = path.join(path.dirname(target), `.${path.basename(target)}.${crypto.randomBytes(8).toString('hex')}.tmp`)
  const fd = fs.openSync(temporary, fs.constants.O_WRONLY | fs.constants.O_CREAT | fs.constants.O_EXCL, 0o600)

  try {
    try {
      fs.writeFileSync(fd, contents, 'utf8')
      fs.fsyncSync(fd)
    } finally {
      fs.closeSync(fd)
    }

    fs.renameSync(temporary, target)

    const directoryFlags = fs.constants.O_RDONLY | (fs.constants.O_DIRECTORY || 0) | (fs.constants.O_NOFOLLOW || 0)
    const directoryFd = fs.openSync(path.dirname(target), directoryFlags)

    try {
      fs.fsyncSync(directoryFd)
    } finally {
      fs.closeSync(directoryFd)
    }
  } catch (error) {
    try {
      fs.unlinkSync(temporary)
    } catch {
      // Preserve the write/rename failure; the private temp file remains owner-only.
    }

    throw error
  }
}

function assertOwnedDirectory(target: string, label: string) {
  const stat = fs.lstatSync(target)

  if (!stat.isDirectory() || stat.isSymbolicLink()) {
    throw new Error(`Local companion runtime ${label} is unsafe`)
  }

  if (typeof process.getuid === 'function' && stat.uid !== process.getuid()) {
    throw new Error(`Local companion runtime ${label} owner mismatch`)
  }
}

function fsyncOwnedDirectory(target: string, label: string) {
  assertOwnedDirectory(target, label)
  const flags = fs.constants.O_RDONLY | (fs.constants.O_DIRECTORY || 0) | (fs.constants.O_NOFOLLOW || 0)
  const fd = fs.openSync(target, flags)

  try {
    fs.fsyncSync(fd)
  } finally {
    fs.closeSync(fd)
  }
}

function readOwnedFileNoFollow(target: string, label: string) {
  const flags = fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW || 0)
  const fd = fs.openSync(target, flags)

  try {
    const stat = fs.fstatSync(fd)

    if (!stat.isFile()) {
      throw new Error(`Local companion runtime ${label} is unsafe`)
    }

    if (typeof process.getuid === 'function' && stat.uid !== process.getuid()) {
      throw new Error(`Local companion runtime ${label} owner mismatch`)
    }

    return fs.readFileSync(fd, 'utf8')
  } finally {
    fs.closeSync(fd)
  }
}

export function resolveLocalCompanionProfile(desktopProfile: string | null | undefined, hermesHome: string) {
  const explicit = String(desktopProfile || '').trim()

  if (PROFILE_NAME_RE.test(explicit)) {
    return explicit
  }

  try {
    const sticky = fs.readFileSync(path.join(hermesHome, 'active_profile'), 'utf8').trim()

    return PROFILE_NAME_RE.test(sticky) ? sticky : 'default'
  } catch {
    return 'default'
  }
}

export function publishLocalCompanionRuntime({
  root = DEFAULT_COMPANION_ROOT,
  profile,
  pid,
  backendPid = pid,
  port,
  token
}: PublishLocalCompanionOptions): LocalCompanionPublication {
  const normalizedProfile = String(profile || '').trim()

  if (!normalizedProfile) {
    throw new Error('Cannot publish a local companion runtime without a profile')
  }

  if (!Number.isInteger(pid) || pid <= 0 || !Number.isInteger(backendPid) || backendPid <= 0) {
    throw new Error('Local companion runtime pids must be positive integers')
  }

  if (!Number.isInteger(port) || port <= 0 || port >= 65536) {
    throw new Error('Local companion runtime port is invalid')
  }

  if (!token) {
    throw new Error('Cannot publish a local companion runtime without a token')
  }

  const resolvedRoot = path.resolve(root)

  fs.mkdirSync(resolvedRoot, { recursive: true, mode: 0o700 })
  assertOwnedDirectory(resolvedRoot, 'root')
  fs.chmodSync(resolvedRoot, 0o700)

  const ownershipId = crypto.randomBytes(16).toString('hex')
  const spawnNonce = crypto.randomBytes(8).toString('hex')
  const directory = path.join(resolvedRoot, ownershipId)
  const markerPath = path.join(directory, 'local-runtime.json')
  const tokenPath = path.join(directory, `${spawnNonce}.token`)
  const lockPath = path.join(directory, 'backend.lock.json')
  const publication = { root: resolvedRoot, ownershipId, spawnNonce, directory, markerPath, lockPath, tokenPath }

  fs.mkdirSync(directory, { mode: 0o700 })
  assertOwnedDirectory(directory, 'directory')
  fs.chmodSync(directory, 0o700)

  try {
    fsyncOwnedDirectory(resolvedRoot, 'root')
    writePrivateAtomic(
      markerPath,
      JSON.stringify({ source: 'local-desktop', ownershipId, spawnNonce, instanceId: LOCAL_DESKTOP_INSTANCE_ID })
    )
    writePrivateAtomic(tokenPath, token)
    writePrivateAtomic(
      lockPath,
      JSON.stringify({
        schemaVersion: LOCKFILE_SCHEMA_VERSION,
        protocolVersion: PROTOCOL_VERSION,
        ownershipId,
        spawnNonce,
        instanceId: LOCAL_DESKTOP_INSTANCE_ID,
        pid,
        backendPid,
        port,
        profile: normalizedProfile,
        tokenFingerprint: crypto.createHash('sha256').update(token).digest('hex').slice(0, 32),
        startedAt: new Date().toISOString(),
        source: 'local-desktop'
      })
    )
  } catch (error) {
    try {
      removeLocalCompanionRuntime(publication)
    } catch (cleanupError) {
      throw new AggregateError([error, cleanupError], 'Local companion runtime publication and cleanup failed')
    }

    throw error
  }

  return publication
}

export function attachLocalCompanionRuntime({
  child,
  ownerPid,
  root,
  profile,
  port,
  token,
  onCleanupError
}: {
  child: {
    pid?: number
    exitCode?: number | null
    once: (event: string, listener: (...args: any[]) => void) => unknown
  }
  ownerPid?: number
  root?: string
  profile: string
  port: number
  token: string
  onCleanupError?: (error: unknown) => void
}) {
  const resolvedRoot = path.resolve(root || DEFAULT_COMPANION_ROOT)
  const stale = reapStaleLocalCompanionRuntimes(resolvedRoot)

  for (const error of stale.errors) {
    onCleanupError?.(error)
  }

  const publication = publishLocalCompanionRuntime({
    root: resolvedRoot,
    profile,
    pid: Number(ownerPid || child.pid || 0),
    backendPid: Number(child.pid || 0),
    port,
    token
  })

  const cleanup = () => {
    liveSpawnNonces.delete(publication.spawnNonce)

    try {
      removeLocalCompanionRuntime(publication)
    } catch (error) {
      onCleanupError?.(error)
    }
  }

  try {
    child.once('error', cleanup)
    child.once('exit', cleanup)

    if (child.exitCode !== null && child.exitCode !== undefined) {
      cleanup()
      throw new Error('Local backend exited before companion publication attached')
    }

    liveSpawnNonces.add(publication.spawnNonce)
  } catch (error) {
    cleanup()
    throw error
  }

  return publication
}

export async function attachLocalCompanionRuntimeOrStop({
  attachment,
  stopChild,
  waitForChildExit
}: {
  attachment: Parameters<typeof attachLocalCompanionRuntime>[0]
  stopChild: () => void
  waitForChildExit: () => Promise<void>
}) {
  try {
    return attachLocalCompanionRuntime(attachment)
  } catch (error) {
    const errors: unknown[] = [error]

    try {
      stopChild()
    } catch (stopError) {
      errors.push(stopError)
    }

    try {
      await waitForChildExit()
    } catch (waitError) {
      errors.push(waitError)
    }

    if (errors.length > 1) {
      throw new AggregateError(errors, 'Companion attachment failed and backend teardown was incomplete')
    }

    throw error
  }
}

export function removeLocalCompanionRuntime(publication: LocalCompanionPublication | null | undefined) {
  if (!publication) {
    return
  }

  if (!/^[0-9a-f]{32}$/.test(publication.ownershipId) || !/^[0-9a-f]{16}$/.test(publication.spawnNonce)) {
    throw new Error('Local companion runtime publication identity is invalid')
  }

  const root = path.resolve(publication.root)
  const directory = path.join(root, publication.ownershipId)
  const markerPath = path.join(directory, 'local-runtime.json')
  const lockPath = path.join(directory, 'backend.lock.json')
  const tokenPath = path.join(directory, `${publication.spawnNonce}.token`)

  if (
    publication.root !== root ||
    publication.directory !== directory ||
    publication.markerPath !== markerPath ||
    publication.lockPath !== lockPath ||
    publication.tokenPath !== tokenPath
  ) {
    throw new Error('Local companion runtime publication paths are invalid')
  }

  try {
    assertOwnedDirectory(root, 'root')
    assertOwnedDirectory(directory, 'directory')
  } catch (error: any) {
    if (error?.code === 'ENOENT') {
      return
    }

    throw error
  }

  const errors: unknown[] = []

  const unlinkOwnedFile = (target: string) => {
    try {
      const stat = fs.lstatSync(target)

      if (!stat.isFile() || stat.isSymbolicLink()) {
        throw new Error('Local companion runtime file is unsafe')
      }

      if (typeof process.getuid === 'function' && stat.uid !== process.getuid()) {
        throw new Error('Local companion runtime file owner mismatch')
      }

      fs.unlinkSync(target)

      return true
    } catch (error: any) {
      if (error?.code === 'ENOENT') {
        return true
      }

      errors.push(error)

      return false
    }
  }

  const tokenRemoved = unlinkOwnedFile(tokenPath)
  let tokenRemovalDurable = tokenRemoved

  if (tokenRemoved) {
    try {
      fsyncOwnedDirectory(directory, 'directory')
    } catch (error) {
      errors.push(error)
      tokenRemovalDurable = false
    }
  }

  unlinkOwnedFile(lockPath)

  // Do not durably discard provenance until token absence is itself durable.
  if (tokenRemovalDurable) {
    unlinkOwnedFile(markerPath)
  }

  try {
    fsyncOwnedDirectory(directory, 'directory')
  } catch (error) {
    errors.push(error)
  }

  let directoryRemoved = false

  try {
    fs.rmdirSync(directory)
    directoryRemoved = true
  } catch (error: any) {
    if (!['ENOENT', 'ENOTEMPTY'].includes(error?.code)) {
      errors.push(error)
    }
  }

  if (directoryRemoved) {
    try {
      fsyncOwnedDirectory(root, 'root')
    } catch (error) {
      errors.push(error)
    }
  }

  if (errors.length > 0) {
    throw new AggregateError(errors, `Local companion runtime cleanup failed: ${errors.map(String).join('; ')}`)
  }
}

export function reapStaleLocalCompanionRuntimes(root = DEFAULT_COMPANION_ROOT) {
  const resolvedRoot = path.resolve(root)
  const result: { removed: number; errors: unknown[] } = { removed: 0, errors: [] }

  try {
    assertOwnedDirectory(resolvedRoot, 'root')
  } catch (error: any) {
    if (error?.code === 'ENOENT') {
      return result
    }

    throw error
  }

  for (const entry of fs.readdirSync(resolvedRoot, { withFileTypes: true })) {
    if (!entry.isDirectory() || !/^[0-9a-f]{32}$/.test(entry.name)) {
      continue
    }

    const directory = path.join(resolvedRoot, entry.name)
    const markerPath = path.join(directory, 'local-runtime.json')
    const lockPath = path.join(directory, 'backend.lock.json')

    try {
      assertOwnedDirectory(directory, 'directory')
      let descriptor: any = null

      try {
        descriptor = JSON.parse(readOwnedFileNoFollow(lockPath, 'lock'))
      } catch {
        descriptor = JSON.parse(readOwnedFileNoFollow(markerPath, 'marker'))
      }

      const instanceId = String(descriptor?.instanceId || '')

      if (
        descriptor?.source !== 'local-desktop' ||
        descriptor?.ownershipId !== entry.name ||
        !/^[0-9a-f]{16}$/.test(String(descriptor?.spawnNonce || '')) ||
        (instanceId && !/^[0-9a-f]{32}$/.test(instanceId))
      ) {
        continue
      }

      if (instanceId === LOCAL_DESKTOP_INSTANCE_ID && liveSpawnNonces.has(String(descriptor.spawnNonce))) {
        continue
      }

      if (!instanceId) {
        writePrivateAtomic(
          markerPath,
          JSON.stringify({
            source: 'local-desktop',
            ownershipId: entry.name,
            spawnNonce: descriptor.spawnNonce,
            instanceId: LOCAL_DESKTOP_INSTANCE_ID
          })
        )
      }

      removeLocalCompanionRuntime({
        root: resolvedRoot,
        ownershipId: entry.name,
        spawnNonce: descriptor.spawnNonce,
        directory,
        markerPath,
        lockPath,
        tokenPath: path.join(directory, `${descriptor.spawnNonce}.token`)
      })
      result.removed += 1
    } catch (error: any) {
      if (error?.code !== 'ENOENT') {
        result.errors.push(error)
      }
    }
  }

  return result
}
