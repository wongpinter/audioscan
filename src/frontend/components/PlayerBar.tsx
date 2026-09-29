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
  useEffect(() => {
    const player = audio.current
    if (!player || !track) return
    player.src = `/api/tracks/${encodeURIComponent(track.id)}/audio`
    setBuffering(true)
    player.load()
    const seek = () => { player.currentTime = start; void player.play().catch(() => setBuffering(false)) }
    const bufferingStart = () => setBuffering(true)
    const bufferingEnd = () => setBuffering(false)
    const loadFailed = () => setBuffering(false)
    player.addEventListener('loadedmetadata', seek, { once: true })
    player.addEventListener('waiting', bufferingStart)
    player.addEventListener('playing', bufferingEnd)
    player.addEventListener('canplay', bufferingEnd)
    player.addEventListener('error', loadFailed)
    return () => {
      player.removeEventListener('loadedmetadata', seek)
      player.removeEventListener('waiting', bufferingStart)
      player.removeEventListener('playing', bufferingEnd)
      player.removeEventListener('canplay', bufferingEnd)
      player.removeEventListener('error', loadFailed)
    }
  }, [track?.id, start])
  return <footer className="player-bar">
    <audio ref={audio} preload="metadata" onTimeUpdate={event => { const value = event.currentTarget.currentTime; setTime(value); onTime(value) }} onDurationChange={event => setDuration(event.currentTarget.duration || 0)} onEnded={onNext} onPlay={() => setPlaying(true)} onPause={() => setPlaying(false)} />
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
