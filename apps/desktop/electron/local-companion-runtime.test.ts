import assert from 'node:assert/strict'
import crypto from 'node:crypto'
import { EventEmitter } from 'node:events'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { afterEach, test, vi } from 'vitest'

import {
  attachLocalCompanionRuntime,
  attachLocalCompanionRuntimeOrStop,
  publishLocalCompanionRuntime,
  reapStaleLocalCompanionRuntimes,
  removeLocalCompanionRuntime,
  resolveLocalCompanionProfile
} from './local-companion-runtime'

const roots: string[] = []

function temporaryRoot() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-local-companion-'))
  roots.push(root)

  return root
}

afterEach(() => {
  vi.restoreAllMocks()

  for (const root of roots.splice(0)) {
    fs.rmSync(root, { recursive: true, force: true })
  }
})

test('publishes a live local backend using the Desktop companion lock contract', () => {
  const root = temporaryRoot()
  const token = 'served-local-token'

  const publication = publishLocalCompanionRuntime({
    root,
    profile: 'claudius',
    pid: 4321,
    port: 54321,
    token
  })

  assert.match(publication.ownershipId, /^[0-9a-f]{32}$/)
  assert.match(publication.spawnNonce, /^[0-9a-f]{16}$/)
  assert.equal(fs.readFileSync(publication.tokenPath, 'utf8'), token)

  const lock = JSON.parse(fs.readFileSync(publication.lockPath, 'utf8'))
  assert.equal(lock.profile, 'claudius')
  assert.equal(lock.pid, 4321)
  assert.equal(lock.port, 54321)
  assert.equal(lock.ownershipId, publication.ownershipId)
  assert.equal(lock.spawnNonce, publication.spawnNonce)
  assert.equal(lock.tokenFingerprint, crypto.createHash('sha256').update(token).digest('hex').slice(0, 32))
  assert.equal(fs.statSync(path.dirname(publication.lockPath)).mode & 0o777, 0o700)
  assert.equal(fs.statSync(publication.tokenPath).mode & 0o777, 0o600)
  assert.equal(fs.statSync(publication.lockPath).mode & 0o777, 0o600)
})

test('publication durably fsyncs the ownership-directory entry in its root', () => {
  const root = temporaryRoot()
  const fsyncSpy = vi.spyOn(fs, 'fsyncSync')

  publishLocalCompanionRuntime({
    root,
    profile: 'claudius',
    pid: process.pid,
    port: 54321,
    token: 'served-local-token'
  })

  assert.ok(fsyncSpy.mock.calls.length >= 7)
})

test('removes private temporary files when a token write fails', () => {
  const root = temporaryRoot()
  vi.spyOn(fs, 'fsyncSync').mockImplementationOnce(() => {
    throw new Error('simulated fsync failure')
  })

  assert.throws(
    () =>
      publishLocalCompanionRuntime({
        root,
        profile: 'claudius',
        pid: 4321,
        port: 54321,
        token: 'served-local-token'
      }),
    /simulated fsync failure/
  )
  assert.deepEqual(fs.readdirSync(root), [])
})

test('removes only the files owned by a local companion publication', () => {
  const root = temporaryRoot()

  const publication = publishLocalCompanionRuntime({
    root,
    profile: 'claudius',
    pid: 4321,
    port: 54321,
    token: 'served-local-token'
  })

  const unrelated = path.join(root, 'unrelated')
  fs.writeFileSync(unrelated, 'keep')

  removeLocalCompanionRuntime(publication)
  removeLocalCompanionRuntime(publication)

  assert.equal(fs.existsSync(publication.lockPath), false)
  assert.equal(fs.existsSync(publication.tokenPath), false)
  assert.equal(fs.readFileSync(unrelated, 'utf8'), 'keep')
})

test('cleanup durably fsyncs publication deletion metadata', () => {
  const root = temporaryRoot()

  const publication = publishLocalCompanionRuntime({
    root,
    profile: 'claudius',
    pid: process.pid,
    port: 54321,
    token: 'served-local-token'
  })

  const fsyncSpy = vi.spyOn(fs, 'fsyncSync')

  removeLocalCompanionRuntime(publication)

  assert.ok(fsyncSpy.mock.calls.length >= 3)
  assert.equal(fs.existsSync(publication.directory), false)
})

test('cleanup still removes the token when unlinking the lock fails', () => {
  const root = temporaryRoot()

  const publication = publishLocalCompanionRuntime({
    root,
    profile: 'claudius',
    pid: process.pid,
    port: 54321,
    token: 'served-local-token'
  })

  const unlinkSync = fs.unlinkSync.bind(fs)
  vi.spyOn(fs, 'unlinkSync').mockImplementation(target => {
    if (String(target) === publication.lockPath) {
      throw new Error('simulated lock unlink failure')
    }

    return unlinkSync(target)
  })

  assert.throws(() => removeLocalCompanionRuntime(publication), /simulated lock unlink failure/)
  assert.equal(fs.existsSync(publication.tokenPath), false)
  assert.equal(fs.existsSync(publication.lockPath), true)
})

test('cleanup preserves provenance when token unlink fails so startup reaping can retry', () => {
  const root = temporaryRoot()

  const publication = publishLocalCompanionRuntime({
    root,
    profile: 'claudius',
    pid: process.pid,
    port: 54321,
    token: 'served-local-token'
  })

  const unlinkSync = fs.unlinkSync.bind(fs)

  const unlinkSpy = vi.spyOn(fs, 'unlinkSync').mockImplementation(target => {
    if (String(target) === publication.tokenPath) {
      throw new Error('simulated token unlink failure')
    }

    return unlinkSync(target)
  })

  assert.throws(() => removeLocalCompanionRuntime(publication), /simulated token unlink failure/)
  assert.equal(fs.existsSync(publication.tokenPath), true)
  assert.equal(fs.existsSync(publication.markerPath), true)
  unlinkSpy.mockRestore()

  const result = reapStaleLocalCompanionRuntimes(root)

  assert.deepEqual(result, { removed: 1, errors: [] })
  assert.equal(fs.existsSync(publication.directory), false)
})

test('reaps untracked publications despite live reused pids but preserves attached ones', () => {
  const root = temporaryRoot()
  const child = new EventEmitter() as EventEmitter & { pid: number }
  child.pid = process.pid

  const live = attachLocalCompanionRuntime({
    child,
    ownerPid: process.pid,
    root,
    profile: 'claudius',
    port: 54322,
    token: 'served-local-token'
  })

  const stale = publishLocalCompanionRuntime({
    root,
    profile: 'claudius',
    pid: process.pid,
    backendPid: process.pid,
    port: 54321,
    token: 'served-local-token'
  })

  const result = reapStaleLocalCompanionRuntimes(root)

  assert.deepEqual(result, { removed: 1, errors: [] })
  assert.equal(fs.existsSync(stale.directory), false)
  assert.equal(fs.existsSync(live.directory), true)
  child.emit('exit', 0, null)
})

test('reaps a token-only crash window using its private ownership marker', () => {
  const root = temporaryRoot()

  const publication = publishLocalCompanionRuntime({
    root,
    profile: 'claudius',
    pid: process.pid,
    port: 54321,
    token: 'served-local-token'
  })

  fs.unlinkSync(publication.lockPath)
  const result = reapStaleLocalCompanionRuntimes(root)

  assert.deepEqual(result, { removed: 1, errors: [] })
  assert.equal(fs.existsSync(publication.tokenPath), false)
  assert.equal(fs.existsSync(publication.directory), false)
})

test('reaps legacy local publications that predate instance markers', () => {
  const root = temporaryRoot()

  const publication = publishLocalCompanionRuntime({
    root,
    profile: 'claudius',
    pid: process.pid,
    port: 54321,
    token: 'served-local-token'
  })

  const lock = JSON.parse(fs.readFileSync(publication.lockPath, 'utf8'))
  delete lock.instanceId
  fs.writeFileSync(publication.lockPath, JSON.stringify(lock), { mode: 0o600 })
  fs.unlinkSync(publication.markerPath)

  const result = reapStaleLocalCompanionRuntimes(root)

  assert.deepEqual(result, { removed: 1, errors: [] })
  assert.equal(fs.existsSync(publication.directory), false)
})

test('rejects a symlinked companion root', () => {
  const parent = temporaryRoot()
  const realRoot = path.join(parent, 'real')
  const linkedRoot = path.join(parent, 'linked')
  fs.mkdirSync(realRoot)
  fs.symlinkSync(realRoot, linkedRoot)

  assert.throws(
    () =>
      publishLocalCompanionRuntime({
        root: linkedRoot,
        profile: 'claudius',
        pid: 4321,
        port: 54321,
        token: 'served-local-token'
      }),
    /root is unsafe/
  )
})

test('cleanup rejects forged paths instead of unlinking unrelated files', () => {
  const root = temporaryRoot()

  const publication = publishLocalCompanionRuntime({
    root,
    profile: 'claudius',
    pid: 4321,
    port: 54321,
    token: 'served-local-token'
  })

  const victim = path.join(root, 'unrelated')
  fs.writeFileSync(victim, 'keep')

  assert.throws(() => removeLocalCompanionRuntime({ ...publication, lockPath: victim }), /paths are invalid/)
  assert.equal(fs.readFileSync(victim, 'utf8'), 'keep')
  assert.equal(fs.existsSync(publication.lockPath), true)
})

test('resolves the actual sticky CLI profile when Desktop has no explicit profile', () => {
  const root = temporaryRoot()
  fs.writeFileSync(path.join(root, 'active_profile'), 'claudius\n')

  assert.equal(resolveLocalCompanionProfile(null, root), 'claudius')
  assert.equal(resolveLocalCompanionProfile('operator', root), 'operator')
})

test('falls back to default when the sticky CLI profile is absent or invalid', () => {
  const root = temporaryRoot()

  assert.equal(resolveLocalCompanionProfile(null, root), 'default')
  fs.writeFileSync(path.join(root, 'active_profile'), '../../escape')
  assert.equal(resolveLocalCompanionProfile(null, root), 'default')
})

test('attached publication is removed when its backend child exits', () => {
  const root = temporaryRoot()
  const child = new EventEmitter() as EventEmitter & { pid: number }
  child.pid = 4321

  const publication = attachLocalCompanionRuntime({
    child,
    ownerPid: 9876,
    root,
    profile: 'claudius',
    port: 54321,
    token: 'served-local-token'
  })

  assert.equal(JSON.parse(fs.readFileSync(publication.lockPath, 'utf8')).pid, 9876)
  assert.equal(fs.existsSync(publication.lockPath), true)
  child.emit('exit', 0, null)
  assert.equal(fs.existsSync(publication.lockPath), false)
  assert.equal(fs.existsSync(publication.tokenPath), false)
})

test('attachment failure removes an already-published token and lock', () => {
  const root = temporaryRoot()

  const child = {
    pid: 4321,
    exitCode: null,
    once: () => {
      throw new Error('simulated listener failure')
    }
  }

  assert.throws(
    () =>
      attachLocalCompanionRuntime({
        child,
        ownerPid: 9876,
        root,
        profile: 'claudius',
        port: 54321,
        token: 'served-local-token'
      }),
    /simulated listener failure/
  )
  assert.deepEqual(fs.readdirSync(root), [])
})

test('attachment fails closed when the child exited before listeners were installed', () => {
  const root = temporaryRoot()
  const child = new EventEmitter() as EventEmitter & { pid: number; exitCode: number }
  child.pid = 4321
  child.exitCode = 1

  assert.throws(
    () =>
      attachLocalCompanionRuntime({
        child,
        ownerPid: 9876,
        root,
        profile: 'claudius',
        port: 54321,
        token: 'served-local-token'
      }),
    /exited before companion publication attached/
  )
  assert.deepEqual(fs.readdirSync(root), [])
})

test('attachment failure stops and awaits the already-running backend', async () => {
  const root = temporaryRoot()
  const calls: string[] = []

  const child = {
    pid: 4321,
    exitCode: null,
    once: () => {
      throw new Error('simulated listener failure')
    }
  }

  await assert.rejects(
    attachLocalCompanionRuntimeOrStop({
      attachment: {
        child,
        ownerPid: process.pid,
        root,
        profile: 'claudius',
        port: 54321,
        token: 'served-local-token'
      },
      stopChild: () => calls.push('stop'),
      waitForChildExit: async () => {
        calls.push('wait')
      }
    }),
    /simulated listener failure/
  )
  assert.deepEqual(calls, ['stop', 'wait'])
  assert.deepEqual(fs.readdirSync(root), [])
})
