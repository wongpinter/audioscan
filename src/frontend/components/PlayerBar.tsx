import { useEffect, useRef, useState, type CSSProperties } from 'react'
import { LoaderCircle, Moon, Pause, Play, RotateCcw, RotateCw, SkipBack, SkipForward, Volume2 } from 'lucide-react'
import type { Book, Track } from '../types'
import { Cover, IconButton } from './ui'

type Props = { book: Book | null; track: Track | null; start: number; canNext: boolean; canPrevious: boolean; onNext: () => void; onPrevious: () => void; onTime: (time: number) => void; onCheckpoint: (time: number) => void }

export function PlayerBar({ book, track, start, canNext, canPrevious, onNext, onPrevious, onTime, onCheckpoint }: Props) {
  const audio = useRef<HTMLAudioElement>(null)
  const [playing, setPlaying] = useState(false)
  const [playError, setPlayError] = useState(false)
  const [buffering, setBuffering] = useState(false)
  const [time, setTime] = useState(0)
  const [duration, setDuration] = useState(0)
  const [playbackRate, setPlaybackRate] = useState('1')
  const [sleepMinutes, setSleepMinutes] = useState('off')
  const [sleepEndsAt, setSleepEndsAt] = useState(0)
  const [sleepRemaining, setSleepRemaining] = useState(0)
  const sleepTimer = useRef<number | null>(null)
  const sleepAtEnd = useRef(false)
  useEffect(() => () => { if (sleepTimer.current) window.clearTimeout(sleepTimer.current) }, [])
  useEffect(() => {
    if (sleepTimer.current) window.clearTimeout(sleepTimer.current)
    sleepTimer.current = null
    sleepAtEnd.current = sleepMinutes === 'end'
    if (sleepMinutes === 'off' || sleepAtEnd.current) { setSleepEndsAt(0); return }
    const end = Date.now() + Number(sleepMinutes) * 60_000
    setSleepEndsAt(end)
    setSleepRemaining(Number(sleepMinutes))
    sleepTimer.current = window.setTimeout(() => { audio.current?.pause(); setSleepMinutes('off'); setSleepEndsAt(0) }, end - Date.now())
    return () => { if (sleepTimer.current) window.clearTimeout(sleepTimer.current) }
  }, [sleepMinutes])
  useEffect(() => {
    if (!sleepEndsAt) return
    const update = () => setSleepRemaining(Math.max(0, Math.ceil((sleepEndsAt - Date.now()) / 60_000)))
    const interval = window.setInterval(update, 10_000)
    return () => window.clearInterval(interval)
  }, [sleepEndsAt])
  const [metrics, setMetrics] = useState({ startupMs: 0, stalls: 0, bufferedAhead: 0, rangeMs: 0, rangeBytes: 0, ranges: 0 })
  const metricsRef = useRef({ requestedAt: 0, startedAt: 0, stalls: 0, rangeMs: 0, rangeBytes: 0, ranges: 0, lastReport: 0, lastSent: 0 })
  useEffect(() => {
    const player = audio.current
    if (!player || !track) return
    const state = metricsRef.current
    Object.assign(state, { requestedAt: performance.now(), startedAt: 0, stalls: 0, rangeMs: 0, rangeBytes: 0, ranges: 0, lastSent: 0 })
    setMetrics({ startupMs: 0, stalls: 0, bufferedAhead: 0, rangeMs: 0, rangeBytes: 0, ranges: 0 })
    player.playbackRate = Number(playbackRate)
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
    const seek = () => { player.currentTime = start; setPlayError(false); void player.play().then(() => setPlayError(false)).catch(() => { setBuffering(false); setPlayError(true) }) }
    const bufferingStart = () => { state.stalls += 1; setBuffering(true); report(true) }
    const bufferingEnd = () => { setBuffering(false); report(true) }
    const playbackStarted = () => { if (!state.startedAt) state.startedAt = performance.now(); report(true) }
    const loadFailed = () => { setBuffering(false); setPlayError(true); report(true) }
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
    <audio ref={audio} preload="metadata" onTimeUpdate={event => { const player = event.currentTarget; const value = player.currentTime; setTime(value); onTime(value); const ahead = player.buffered.length ? Math.max(0, player.buffered.end(player.buffered.length - 1) - value) : 0; setMetrics(current => ({ ...current, bufferedAhead: Math.round(ahead) })) }} onDurationChange={event => setDuration(event.currentTarget.duration || 0)} onEnded={event => { onCheckpoint(event.currentTarget.currentTime); if (sleepAtEnd.current) { sleepAtEnd.current = false; setSleepMinutes('off'); setSleepEndsAt(0) } else onNext() }} onPause={event => { onCheckpoint(event.currentTarget.currentTime); setPlaying(false) }} onPlay={() => setPlaying(true)} />
    {playError && <button className="play-retry" onClick={() => { const player = audio.current; if (!player) return; const position = player.currentTime || start; player.src = `/api/tracks/${encodeURIComponent(track?.id ?? '')}/audio`; player.load(); player.addEventListener('loadedmetadata', () => { player.currentTime = position; void player.play().then(() => setPlayError(false)).catch(() => setPlayError(true)) }, { once: true }) }}>Retry audio</button>}
    {import.meta.env.DEV && <output className="playback-metrics">Start {metrics.startupMs} ms · stalls {metrics.stalls} · buffer {metrics.bufferedAhead}s · Drive {metrics.rangeMs} ms / {metrics.ranges} ranges / {metrics.rangeBytes} B</output>}
    <div className="player-track">{book?.cover && <Cover src={book.cover} alt="" />}
      <div className="player-label"><strong>{track?.title ?? 'Choose a book'}</strong><span>{buffering ? 'Buffering audio…' : book?.title ?? 'Audiobooks'}</span></div>
    </div>
    <label className="playback-speed-control"><span>Speed</span><select aria-label="Playback speed" value={playbackRate} onChange={event => { setPlaybackRate(event.target.value); if (audio.current) audio.current.playbackRate = Number(event.target.value) }}><option value="0.75">0.75×</option><option value="1">1×</option><option value="1.25">1.25×</option><option value="1.5">1.5×</option><option value="1.75">1.75×</option><option value="2">2×</option></select></label>
    <label className="sleep-timer"><Moon size={16} /><select aria-label="Sleep timer" value={sleepMinutes} onChange={event => setSleepMinutes(event.target.value)}><option value="off">Timer off</option><option value="15">15 min</option><option value="30">30 min</option><option value="45">45 min</option><option value="60">60 min</option><option value="end">End of track</option></select>{sleepEndsAt > 0 && <output aria-live="polite">{sleepRemaining}m</output>}</label>
    <div className="player-controls">
      <IconButton className="track-skip" aria-label="Previous track" disabled={!canPrevious} onClick={onPrevious}><SkipBack size={18} /></IconButton>
      <IconButton className="seek-skip" aria-label="Back 15 seconds" disabled={!track} onClick={() => { if (audio.current) audio.current.currentTime = Math.max(0, audio.current.currentTime - 15) }}><RotateCcw size={17} /><span>15</span></IconButton>
      <IconButton aria-label={buffering ? 'Buffering audio' : playing ? 'Pause' : 'Play'} className="play-toggle" disabled={!track || buffering} onClick={() => { const p = audio.current; if (p?.paused) void p.play(); else p?.pause() }}>{buffering ? <LoaderCircle className="spin" size={18} /> : playing ? <Pause size={18} fill="currentColor" /> : <Play size={18} fill="currentColor" />}</IconButton>
      <IconButton className="seek-skip" aria-label="Forward 15 seconds" disabled={!track} onClick={() => { if (audio.current) audio.current.currentTime = Math.min(duration, audio.current.currentTime + 15) }}><RotateCw size={17} /><span>15</span></IconButton>
      <IconButton className="track-skip" aria-label="Next track" disabled={!canNext} onClick={onNext}><SkipForward size={18} /></IconButton>
    </div>
    <div className="player-timeline"><span>{formatTime(time)}</span><input aria-label="Playback position" aria-valuetext={`${formatTime(time)} of ${formatTime(duration)}`} type="range" min="0" max={duration || 100} value={Math.min(time, duration || 100)} style={{ '--played': `${duration ? Math.min(100, time / duration * 100) : 0}%` } as CSSProperties} onChange={event => { if (audio.current) audio.current.currentTime = Number(event.target.value) }} /><span>{formatTime(duration)}</span></div>
    <Volume2 className="volume-icon" size={18} />
  </footer>
}

function formatTime(value: number) {
  const seconds = Math.floor(Number.isFinite(value) ? value : 0)
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`
}
