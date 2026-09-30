import type { Book, Progress, Track } from './types'

export type Checkpoint = { bookId: string; trackId: string; position: number; savedAt: number }

export function resumeTrack(book: Book): Track | undefined {
  return book.tracks.find(track => track.id === book.progress?.track_id) ?? book.tracks[0]
}

export function listeningStatus(book: Book): 'in-progress' | 'not-started' | 'completed' {
  const index = book.tracks.findIndex(track => track.id === book.progress?.track_id)
  if (index < 0) return 'not-started'
  const position = book.progress?.position ?? 0
  const track = book.tracks[index]
  if (index === book.tracks.length - 1 && track.duration > 0 && position / track.duration >= 0.95) return 'completed'
  return index > 0 || position > 0 ? 'in-progress' : 'not-started'
}

export function newerCheckpoint(
  book: { id: string; tracks: { id: string }[]; progress?: Progress | null },
  checkpoint: Checkpoint | null,
): Checkpoint | null {
  if (!checkpoint || checkpoint.bookId !== book.id || !book.tracks.some(track => track.id === checkpoint.trackId)) return null
  if (!Number.isFinite(checkpoint.savedAt) || !Number.isFinite(checkpoint.position) || checkpoint.position < 0) return null
  // Older SQLite timestamps are UTC even though they omit the timezone.
  const timestamp = book.progress?.updated_at
  const serverTime = timestamp ? Date.parse(timestamp.endsWith('Z') ? timestamp : `${timestamp.replace(' ', 'T')}Z`) : 0
  return Number.isFinite(serverTime) && checkpoint.savedAt > serverTime ? checkpoint : null
}

export function persistCheckpoint(checkpoint: Checkpoint, keepalive = false): Promise<Response> {
  return fetch(`/api/progress/${encodeURIComponent(checkpoint.bookId)}`, {
    method: 'PUT',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ track_id: checkpoint.trackId, position: checkpoint.position }),
    keepalive,
  })
}
