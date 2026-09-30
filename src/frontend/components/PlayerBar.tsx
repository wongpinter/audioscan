import { useEffect, useRef, useState } from 'react'
import { LoaderCircle, Pause, Play, SkipBack, SkipForward, Volume2 } from 'lucide-react'
import type { Book, Track } from '../types'
import { Cover, IconButton } from './ui'

type Props = { book: Book | null; track: Track | null; start: number; onNext: () => void; onPrevious: () => void; onTime: (time: number) => void }

export function PlayerBar({ book, track, start, onNext, onPrevious, onTime }: Props) {
  const audio = useRef<HTMLAudioElement>(null)
  const [playing, setPlaying] = useState(false)
  const [buffering, setBuffering] = useState(false)
  const [time, setTime] = useState(0)
  const [duration, setDuration] = useState(0)
  const [metrics, setMetrics] = useState({ startupMs: 0, stalls: 0, bufferedAhead: 0, rangeMs: 0, rangeBytes: 0, ranges: 0 })
  const metricsRef = useRef({ requestedAt: 0, startedAt: 0, stalls: 0, rangeMs: 0, rangeBytes: 0, ranges: 0, lastReport: 0, lastSent: 0 })
  useEffect(() => {
    const player = audio.current
    if (!player || !track) return
    const state = metricsRef.current
    Object.assign(state, { requestedAt: performance.now(), startedAt: 0, stalls: 0, rangeMs: 0, rangeBytes: 0, ranges: 0, lastSent: 0 })
    setMetrics({ startupMs: 0, stalls: 0, bufferedAhead: 0, rangeMs: 0, rangeBytes: 0, ranges: 0 })
    player.src = `/api/tracks/${encodeURIComponent(track.id)}/audio`
    setBuffering(true)
    player.load()
    const report = (force = false) => {
      const now = performance.now()
      if (!force && now - state.lastReport < 5000) return
      state.lastReport = now
      const ahead = player.buffered.length ? Math.max(0, player.buffered.end(player.buffered.length - 1) - player.currentTime) : 0
      const snapshot = { startupMs: state.startedAt ? Math.round(state.startedAt - state.requestedAt) : 0, stalls: state.stalls, bufferedAhead: Math.round(ahead), rangeMs: Math.round(state.rangeMs), rangeBytes: state.rangeBytes, ranges: state.ranges }
      setMetrics(snapshot)
      if (state.startedAt && now - state.lastSent >= 30_000) {
        state.lastSent = now
        void fetch(`/api/tracks/${encodeURIComponent(track.id)}/playback-metrics`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(snapshot), keepalive: true })
      }
    }
    const seek = () => { player.currentTime = start; void player.play().catch(() => setBuffering(false)) }
    const bufferingStart = () => { state.stalls += 1; setBuffering(true); report(true) }
    const bufferingEnd = () => { setBuffering(false); report(true) }
    const playbackStarted = () => { if (!state.startedAt) state.startedAt = performance.now(); report(true) }
    const loadFailed = () => { setBuffering(false); report(true) }
    const observeResources = new PerformanceObserver(list => {
      for (const entry of list.getEntries() as PerformanceResourceTiming[]) {
        if (!entry.name.includes(`/api/tracks/${encodeURIComponent(track.id)}/audio`)) continue
        state.ranges += 1
        state.rangeMs += Math.max(0, entry.responseEnd - entry.requestStart)
        state.rangeBytes += entry.transferSize || 0
        report()
      }
    })
    observeResources.observe({ type: 'resource', buffered: true })
    player.addEventListener('loadedmetadata', seek, { once: true })
    player.addEventListener('waiting', bufferingStart)
    player.addEventListener('playing', bufferingEnd)
    player.addEventListener('playing', playbackStarted)
    player.addEventListener('canplay', bufferingEnd)
    player.addEventListener('error', loadFailed)
    return () => {
      observeResources.disconnect()
      report(true)
      if (state.startedAt && performance.now() - state.lastSent >= 30_000) {
        state.lastSent = performance.now()
        void fetch(`/api/tracks/${encodeURIComponent(track.id)}/playback-metrics`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ startupMs: Math.round(state.startedAt - state.requestedAt), stalls: state.stalls, bufferedAhead: metrics.bufferedAhead, rangeMs: Math.round(state.rangeMs), rangeBytes: state.rangeBytes, ranges: state.ranges }), keepalive: true })
      }
      player.removeEventListener('loadedmetadata', seek)
      player.removeEventListener('waiting', bufferingStart)
      player.removeEventListener('playing', bufferingEnd)
      player.removeEventListener('playing', playbackStarted)
      player.removeEventListener('canplay', bufferingEnd)
      player.removeEventListener('error', loadFailed)
    }
  }, [track?.id, start])
  return <footer className="player-bar">
    <audio ref={audio} preload="metadata" onTimeUpdate={event => { const player = event.currentTarget; const value = player.currentTime; setTime(value); onTime(value); const ahead = player.buffered.length ? Math.max(0, player.buffered.end(player.buffered.length - 1) - value) : 0; setMetrics(current => ({ ...current, bufferedAhead: Math.round(ahead) })) }} onDurationChange={event => setDuration(event.currentTarget.duration || 0)} onEnded={onNext} onPlay={() => setPlaying(true)} onPause={() => setPlaying(false)} />
    {import.meta.env.DEV && <output className="playback-metrics">Start {metrics.startupMs} ms · stalls {metrics.stalls} · buffer {metrics.bufferedAhead}s · Drive {metrics.rangeMs} ms / {metrics.ranges} ranges / {metrics.rangeBytes} B</output>}
    <div className="player-track">{book?.cover && <Cover src={book.cover} alt="" />}
      <div className="player-label"><strong>{track?.title ?? 'Choose a book'}</strong><span>{buffering ? 'Buffering audio…' : book?.title ?? 'Audiobooks'}</span></div>
    </div>
    <div className="player-controls">
      <IconButton aria-label="Previous track" onClick={onPrevious}><SkipBack size={18} /></IconButton>
      <IconButton aria-label={buffering ? 'Buffering audio' : playing ? 'Pause' : 'Play'} className="play-toggle" disabled={!track || buffering} onClick={() => { const p = audio.current; if (p?.paused) void p.play(); else p?.pause() }}>{buffering ? <LoaderCircle className="spin" size={18} /> : playing ? <Pause size={18} fill="currentColor" /> : <Play size={18} fill="currentColor" />}</IconButton>
      <IconButton aria-label="Next track" onClick={onNext}><SkipForward size={18} /></IconButton>
    </div>
    <div className="player-timeline"><span>{formatTime(time)}</span><input aria-label="Playback position" type="range" min="0" max={duration || 100} value={Math.min(time, duration || 100)} onChange={event => { if (audio.current) audio.current.currentTime = Number(event.target.value) }} /><span>{formatTime(duration)}</span></div>
    <Volume2 className="volume-icon" size={18} />
  </footer>
}

function formatTime(value: number) {
  const seconds = Math.floor(Number.isFinite(value) ? value : 0)
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`
}
