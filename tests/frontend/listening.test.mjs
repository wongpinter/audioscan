import assert from 'node:assert/strict'
import test from 'node:test'
import { resumeTrack, listeningStatus, newerCheckpoint, persistCheckpoint } from '../../src/frontend/listening.ts'

const book = { id: 'book', tracks: [{ id: 'first', duration: 100 }, { id: 'last', duration: 100 }], progress: { track_id: 'last', position: 30 } }

test('resume selects saved part and falls back when it was removed', () => {
  assert.equal(resumeTrack(book).id, 'last')
  assert.equal(resumeTrack({ ...book, progress: { track_id: 'removed', position: 30 } }).id, 'first')
})

test('completion requires the final part; zero seconds on later parts is in progress', () => {
  assert.equal(listeningStatus({ ...book, progress: { track_id: 'first', position: 99 } }), 'in-progress')
  assert.equal(listeningStatus({ ...book, progress: { track_id: 'last', position: 0 } }), 'in-progress')
  assert.equal(listeningStatus({ ...book, progress: { track_id: 'last', position: 99 } }), 'completed')
  assert.equal(listeningStatus({ ...book, progress: null }), 'not-started')
})

test('server progress takes precedence over older or undated local checkpoints', () => {
  const serverBook = { ...book, progress: { ...book.progress, updated_at: '2026-09-30T10:00:00.000Z' } }
  const checkpoint = { bookId: 'book', trackId: 'last', position: 80, savedAt: Date.parse('2026-09-30T09:00:00Z') }
  assert.equal(newerCheckpoint(serverBook, checkpoint), null)
  assert.equal(newerCheckpoint(serverBook, { ...checkpoint, savedAt: undefined }), null)
  const newer = { ...checkpoint, savedAt: Date.parse('2026-09-30T11:00:00Z') }
  assert.equal(newerCheckpoint(serverBook, newer), newer)
  assert.equal(newerCheckpoint({ ...serverBook, progress: { ...book.progress, updated_at: '2026-09-30 12:00:00' } }, newer), null)
  assert.equal(newerCheckpoint(serverBook, { ...newer, trackId: 'removed' }), null)
})


test('page-close progress uses a keepalive PUT matching the API', async () => {
  const original = globalThis.fetch
  let captured
  globalThis.fetch = async (url, options) => { captured = { url, options }; return new Response('{}') }
  try {
    await persistCheckpoint({ bookId: 'book/a', trackId: 'last', position: 32, savedAt: Date.now() }, true)
    assert.equal(captured.url, '/api/progress/book%2Fa')
    assert.equal(captured.options.method, 'PUT')
    assert.equal(captured.options.keepalive, true)
    assert.deepEqual(JSON.parse(captured.options.body), { track_id: 'last', position: 32 })
  } finally { globalThis.fetch = original }
})
