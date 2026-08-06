import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'

import { test } from 'vitest'

import { waitForBackendExit } from './backend-child'

function fakeChild() {
  const child = new EventEmitter() as EventEmitter & {
    exitCode: number | null
    signalCode: string | null
    pid: number
    kill: (signal: string) => void
  }

  child.exitCode = null
  child.signalCode = null
  child.pid = 4321

  child.kill = () => {}

  return child
}

test('waitForBackendExit does not resolve merely because hard kill was sent', async () => {
  const child = fakeChild()
  const calls: string[] = []
  let settled = false

  const waiting = waitForBackendExit(child, {
    timeoutMs: 5,
    hardKillGraceMs: 100,
    isWindows: false,
    forceKillProcessTree: () => {},
    onHardKill: () => calls.push('kill')
  }).then(() => {
    settled = true
  })

  await new Promise(resolve => setTimeout(resolve, 15))
  assert.deepEqual(calls, ['kill'])
  assert.equal(settled, false)

  child.exitCode = 0
  child.emit('exit', 0, null)
  await waiting
  assert.equal(settled, true)
})

test('waitForBackendExit rejects when exit is still unobserved after hard kill', async () => {
  const child = fakeChild()

  await assert.rejects(
    waitForBackendExit(child, {
      timeoutMs: 5,
      hardKillGraceMs: 5,
      isWindows: false,
      forceKillProcessTree: () => {}
    }),
    /did not exit after hard kill/
  )
})
