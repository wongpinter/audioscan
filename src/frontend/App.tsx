import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from 'react'
import { Link, Route, Routes, useLocation, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { ArrowLeft, BookOpen, CheckCircle2, Clock3, Headphones, Library, LoaderCircle, Search, Settings2, SlidersHorizontal, Heart, Star, Plus } from 'lucide-react'
import { Avatar, Button, Card, Cover, Input, PlayIcon, Progress, Skeleton } from './components/ui'
import { PlayerBar } from './components/PlayerBar'
import type { Book, Chapter, Track } from './types'

type ScanStatus = { status: string; total: number; processed: number; current: string; error: string }
type Features = { favorites: string[]; ratings: Record<string, number>; tags: Record<string, string[]>; playlists: { id: string; name: string; book_ids: string[] }[]; history: { book_id: string; track_id: string; position: number; played_at: string }[] }
type StorageInfo = { tracks: number; bytes: number; formats: { mime_type: string; count: number }[] }

async function api<T>(url: string, options?: RequestInit): Promise<T> {
  const response = await fetch(url, { credentials: 'same-origin', ...options })
  if (!response.ok) throw new Error(response.status === 401 ? 'Sign in to open your library.' : await response.text())
  return response.json() as Promise<T>
}

function App() {
  const [books, setBooks] = useState<Book[]>([])
  const [filter, setFilter] = useState('')
  const [statusFilter, setStatusFilter] = useState<'all' | 'in-progress' | 'not-started' | 'completed'>('all')
  const [sortBy, setSortBy] = useState<'title' | 'author' | 'duration' | 'rating'>('title')
  const [groupMode, setGroupMode] = useState<'artist' | 'directory' | 'album'>('artist')
  const [favoritesOnly, setFavoritesOnly] = useState(false)
  const [playlistView, setPlaylistView] = useState<string | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [scan, setScan] = useState<ScanStatus | null>(null)
  const [features, setFeatures] = useState<Features>({ favorites: [], ratings: {}, tags: {}, playlists: [], history: [] })
  const [storage, setStorage] = useState<StorageInfo | null>(null)
  const [featureError, setFeatureError] = useState('')
  const [activeBook, setActiveBook] = useState<Book | null>(null)
  const [activeTrack, setActiveTrack] = useState<Track | null>(null)
  const [startAt, setStartAt] = useState(0)
  const [resumeBook, setResumeBook] = useState<Book | null>(null)
  const navigate = useNavigate()
  const progressState = useRef({ lastSentAt: 0, latest: null as { bookId: string; trackId: string; position: number } | null })
  const prefetchedTracks = useRef(new Set<string>())
  const currentListen = useRef<{ bookId: string; trackId: string } | null>(null)

  const load = useCallback(async () => {
    try {
      const [result, recent] = await Promise.all([api<Book[]>('/api/library'), api<Features>('/api/features')])
      setBooks(result)
      const lastPlayed = recent.history[0]?.book_id
      let saved: { bookId: string; trackId: string; position: number } | null = null
      try { saved = JSON.parse(localStorage.getItem('ruangdengar.last-listening') || 'null') } catch { /* storage can be disabled */ }
      const savedBook = saved && result.find(item => item.id === saved?.bookId && item.tracks.some(track => track.id === saved?.trackId))
      let pending: { bookId: string; trackId: string; position: number } | null = null
      try { pending = JSON.parse(localStorage.getItem('ruangdengar.pending-progress') || 'null') } catch { /* storage can be disabled */ }
      const book = savedBook || result.find(item => item.id === lastPlayed)
      const pendingMatchesSaved = !!(saved && pending?.bookId === saved.bookId && pending.trackId === saved.trackId)
      const recovery = savedBook
        ? { ...savedBook, progress: { track_id: saved!.trackId, position: pendingMatchesSaved ? pending!.position : saved!.position } }
        : book?.progress?.track_id ? book : result.find(item => (item.progress?.position ?? 0) > 0 && item.tracks.some(track => track.id === item.progress?.track_id))
      setResumeBook(recovery || null)
      result.filter(book => (book.progress?.position ?? 0) > 0).slice(0, 3).forEach(book => { const track = book.tracks.find(item => item.id === book.progress?.track_id); if (track) void fetch(`/api/tracks/${encodeURIComponent(track.id)}/warm`, { method: 'POST', credentials: 'same-origin' }).catch(() => {}) })
      setError('')
    } catch (e) { setError(e instanceof Error ? e.message : 'Could not load library.') }
    finally { setLoading(false) }
  }, [])
  const loadFeatures = useCallback(async () => {
    try { const [data, info] = await Promise.all([api<Features>('/api/features'), api<StorageInfo>('/api/storage')]); setFeatures(data); setStorage(info); setFeatureError('') }
    catch (e) { setFeatureError(e instanceof Error ? e.message : 'Could not load library tools.') }
  }, [])
  useEffect(() => { void load(); void loadFeatures() }, [load, loadFeatures])
  const refreshLibrary = useCallback(async () => {
    try {
      const response = await fetch('/api/library/refresh', { method: 'POST', credentials: 'same-origin' })
      if (!response.ok && response.status !== 409) throw new Error(await response.text())
      setScan(previous => ({ status: 'running', total: previous?.total ?? 0, processed: previous?.processed ?? 0, current: 'Connecting to Google Drive', error: '' }))
    } catch (e) { setError(e instanceof Error ? e.message : 'Could not start library scan.') }
  }, [])
  useEffect(() => {
    let events: EventSource | null = null
    let disposed = false
    void api<ScanStatus>('/api/library/scan').then(status => {
      if (disposed) return
      setScan(status)
      if (status.status === 'running') {
        events = new EventSource('/api/library/events')
        events.onmessage = event => {
          const next = JSON.parse(event.data) as ScanStatus
          setScan(next)
          if (next.status === 'completed' || next.status === 'failed') { events?.close(); void load() }
          else if (next.processed > 0 && next.processed % 50 === 0) void load()
        }
      }
    }).catch(() => {})
    return () => { disposed = true; events?.close() }
  }, [load])
  const filtered = useMemo(() => books
    .filter(book => !favoritesOnly || features.favorites.includes(book.id))
    .filter(book => !playlistView || features.playlists.find(item => item.id === playlistView)?.book_ids.includes(book.id))
    .filter(book => `${book.title} ${book.artist ?? ''} ${(features.tags[book.id] ?? []).join(' ')}`.toLowerCase().includes(filter.trim().toLowerCase()))
    .filter(book => {
      const progress = book.progress?.position ?? 0
      const currentTrack = book.tracks.find(track => track.id === book.progress?.track_id)
      const ratio = currentTrack?.duration ? progress / currentTrack.duration : 0
      if (statusFilter === 'in-progress') return progress > 0 && ratio < 0.95
      if (statusFilter === 'not-started') return progress <= 0
      if (statusFilter === 'completed') return ratio >= 0.95
      return true
    })
    .sort((a, b) => sortBy === 'author'
      ? (a.artist ?? '').localeCompare(b.artist ?? '') || a.title.localeCompare(b.title)
      : sortBy === 'duration' ? b.duration - a.duration : sortBy === 'rating' ? (features.ratings[b.id] ?? 0) - (features.ratings[a.id] ?? 0) : a.title.localeCompare(b.title)),
  [books, filter, statusFilter, sortBy, features, favoritesOnly, playlistView])
  const play = (book: Book, track: Track, start = 0) => {
    currentListen.current = { bookId: book.id, trackId: track.id }
    try { localStorage.setItem('ruangdengar.last-listening', JSON.stringify({ bookId: book.id, trackId: track.id, position: start })) } catch { /* storage can be disabled */ }
    setResumeBook(null)
    void fetch(`/api/history/${encodeURIComponent(book.id)}`, { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ track_id: track.id }) })
    void fetch(`/api/tracks/${encodeURIComponent(track.id)}/warm`, { method: 'POST', credentials: 'same-origin' }).catch(() => {})
    setActiveBook(book); setActiveTrack(track); setStartAt(start)
    void loadFeatures()
  }
  const trackQueue = useMemo(() => {
    if (!activeBook || !activeTrack) return []
    const album = activeBook.album?.trim()
    const queue = album
      ? books.flatMap(book => book.album?.trim() === album && book.artist === activeBook.artist ? book.tracks.map(track => ({ book, track })) : [])
      : activeBook.tracks.map(track => ({ book: activeBook, track }))
    return queue.some(item => item.track.id === activeTrack.id) ? queue : activeBook.tracks.map(track => ({ book: activeBook, track }))
  }, [activeBook, activeTrack, books])
  const queueIndex = trackQueue.findIndex(item => item.track.id === activeTrack?.id)
  const moveTrack = (delta: number) => {
    const index = queueIndex
    if (index < 0 || !trackQueue.length) return
    const next = trackQueue[index + delta]
    if (next) play(next.book, next.track)
  }
  const updateBookFeature = async (bookId: string, kind: 'favorite' | 'rating' | 'tags', value: boolean | number | string[]) => {
    const result = await api<Record<string, unknown>>(`/api/books/${encodeURIComponent(bookId)}/${kind}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ [kind]: value }) })
    if (kind === 'favorite') setFeatures(previous => ({ ...previous, favorites: value ? [...new Set([...previous.favorites, bookId])] : previous.favorites.filter(id => id !== bookId) }))
    if (kind === 'rating') setFeatures(previous => { const ratings = { ...previous.ratings }; if (result.rating) ratings[bookId] = result.rating as number; else delete ratings[bookId]; return { ...previous, ratings } })
    if (kind === 'tags') setFeatures(previous => ({ ...previous, tags: { ...previous.tags, [bookId]: result.tags as string[] } }))
  }
  const makePlaylist = async () => {
    const name = window.prompt('Playlist name')?.trim()
    if (!name) return
    try { const playlist = await api<Features['playlists'][number]>('/api/playlists', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name }) }); setFeatures(previous => ({ ...previous, playlists: [...previous.playlists, playlist] })) }
    catch (e) { setFeatureError(e instanceof Error ? e.message : 'Could not create playlist.') }
  }
  const addToPlaylist = async (playlistId: string, bookId: string) => {
    const playlist = features.playlists.find(item => item.id === playlistId)
    if (!playlist || playlist.book_ids.includes(bookId)) return
    try { const result = await api<{ book_ids: string[] }>(`/api/playlists/${playlistId}/books`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ book_ids: [...playlist.book_ids, bookId] }) }); setFeatures(previous => ({ ...previous, playlists: previous.playlists.map(item => item.id === playlistId ? { ...item, book_ids: result.book_ids } : item) })) }
    catch (e) { setFeatureError(e instanceof Error ? e.message : 'Could not update playlist.') }
  }
  const saveProgress = useCallback((time: number, force = false) => {
    if (activeBook && activeTrack?.duration && time / activeTrack.duration >= 0.8) {
      const index = activeBook.tracks.findIndex(track => track.id === activeTrack.id)
      const next = activeBook.tracks[index + 1]
      if (next && !prefetchedTracks.current.has(next.id)) {
        prefetchedTracks.current.add(next.id)
        void fetch(`/api/tracks/${encodeURIComponent(next.id)}/warm`, { method: 'POST', credentials: 'same-origin' }).catch(() => {})
      }
    }
    if (!activeBook || !activeTrack || time <= 0) return
    const checkpoint = { bookId: activeBook.id, trackId: activeTrack.id, position: Math.floor(time) }
    if (currentListen.current?.bookId === checkpoint.bookId && currentListen.current.trackId === checkpoint.trackId) {
      try { localStorage.setItem('ruangdengar.last-listening', JSON.stringify(checkpoint)) } catch { /* storage can be disabled */ }
    }
    progressState.current.latest = checkpoint
    try { localStorage.setItem('ruangdengar.pending-progress', JSON.stringify(checkpoint)) } catch { /* storage can be disabled */ }
    if (!force && Date.now() - progressState.current.lastSentAt < 15_000) return
    progressState.current.lastSentAt = Date.now()
    void fetch(`/api/progress/${encodeURIComponent(checkpoint.bookId)}`, { method: 'PUT', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ track_id: checkpoint.trackId, position: checkpoint.position }) }).then(response => {
      if (!response.ok) throw new Error(`Progress save failed: ${response.status}`)
      if (progressState.current.latest === checkpoint) { try { localStorage.removeItem('ruangdengar.pending-progress') } catch { /* storage can be disabled */ } }
    }).catch(() => {})
  }, [activeBook, activeTrack])
  useEffect(() => {
    const flush = () => {
      const checkpoint = progressState.current.latest
      if (!checkpoint) return
      const body = JSON.stringify({ track_id: checkpoint.trackId, position: checkpoint.position })
      if (navigator.sendBeacon?.(`/api/progress/${encodeURIComponent(checkpoint.bookId)}`, new Blob([body], { type: 'application/json' }))) return
      void fetch(`/api/progress/${encodeURIComponent(checkpoint.bookId)}`, { method: 'PUT', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body, keepalive: true }).catch(() => {})
    }
    const retry = () => {
      let pending: { bookId: string; trackId: string; position: number } | null = null
      try { pending = JSON.parse(localStorage.getItem('ruangdengar.pending-progress') || 'null') } catch { return }
      if (!pending) return
      void fetch(`/api/progress/${encodeURIComponent(pending.bookId)}`, { method: 'PUT', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ track_id: pending.trackId, position: pending.position }) }).then(response => { if (response.ok) localStorage.removeItem('ruangdengar.pending-progress') }).catch(() => {})
    }
    window.addEventListener('pagehide', flush)
    window.addEventListener('online', retry)
    document.addEventListener('visibilitychange', flush)
    retry()
    return () => { window.removeEventListener('pagehide', flush); window.removeEventListener('online', retry); document.removeEventListener('visibilitychange', flush) }
  }, [])

  const openBook = (book: Book) => navigate(`/book/${encodeURIComponent(book.id)}`)
  const playBook = (book: Book) => { const track = book.tracks[0]; if (track) play(book, track, book.progress?.track_id === track.id ? book.progress.position : 0) }
  const groupLink = (mode: 'artist' | 'directory' | 'album', name: string) => navigate(`/group/${mode}?name=${encodeURIComponent(name)}`)
  return <div className="app-shell">
    <header className="app-header">
      <Link className="brand" to="/"><span className="brand-mark"><Headphones size={19} /></span><span>Ruang<span className="brand-accent">Dengar</span></span></Link>
      <nav className="desktop-nav" aria-label="Main navigation"><Link to="/">Home</Link><Link to="/search">Search</Link><Link to="/library">Library</Link></nav>
      <div className="header-actions"><Button className="scan-action" disabled={scan?.status === 'running'} onClick={() => void refreshLibrary()}>{scan?.status === 'running' ? 'Scanning…' : 'Scan Drive'}</Button><Link className="settings-link" to="/settings" aria-label="Settings"><Settings2 size={19} /></Link></div>
    </header>
    <main className="main-content"><Routes>
      <Route path="/" element={<HomePage books={filtered} features={features} scan={scan} error={error} onOpen={openBook} onPlay={playBook} onLibrary={() => navigate('/library')} onRefresh={() => void refreshLibrary()} />} />
      <Route path="/search" element={<SearchPage books={filtered} query={filter} setQuery={setFilter} onOpen={openBook} />} />
      <Route path="/library" element={<LibraryPage groupMode={groupMode} setGroupMode={setGroupMode} onOpenGroup={groupLink} books={filtered} allBooks={books} features={features} storage={storage} featureError={featureError} onFeature={updateBookFeature} favoritesOnly={favoritesOnly} setFavoritesOnly={setFavoritesOnly} onCreatePlaylist={makePlaylist} onAddToPlaylist={addToPlaylist} onPlaylistView={setPlaylistView} playlistView={playlistView} reloadFeatures={loadFeatures} query={filter} setQuery={setFilter} statusFilter={statusFilter} setStatusFilter={setStatusFilter} sortBy={sortBy} setSortBy={setSortBy} error={error} loading={loading} scan={scan} onRefresh={() => void refreshLibrary()} onOpen={openBook} onPlay={playBook} />} />
      <Route path="/group/:mode" element={<GroupPage books={filtered} onOpen={openBook} onOpenGroup={groupLink} />} />
      <Route path="/book/:bookId" element={<BookPage onPlay={play} />} />
      <Route path="/settings" element={<SettingsPage reloadLibrary={load} />} />
      <Route path="*" element={<div className="empty-state">Page not found.</div>} />
    </Routes></main>
    <BottomNavigation />
    {resumeBook && !activeTrack && <button className="resume-player" onClick={() => { const track = resumeBook.tracks.find(item => item.id === resumeBook.progress?.track_id); if (track) { play(resumeBook, track, resumeBook.progress?.position ?? 0); setResumeBook(null) } }}>Resume listening · {resumeBook.title}</button>}
    <PlayerBar book={activeBook} track={activeTrack} start={startAt} canNext={queueIndex >= 0 && queueIndex < trackQueue.length - 1} canPrevious={queueIndex > 0} onNext={() => moveTrack(1)} onPrevious={() => moveTrack(-1)} onTime={saveProgress} onCheckpoint={time => saveProgress(time, true)} />
  </div>
}

function BottomNavigation() {
  const { pathname } = useLocation()
  return <nav className="bottom-navigation" aria-label="Main navigation">
    <Link to="/" aria-current={pathname === '/' ? 'page' : undefined}><Headphones size={19} /><span>Home</span></Link>
    <Link to="/search" aria-current={pathname === '/search' ? 'page' : undefined}><Search size={19} /><span>Search</span></Link>
    <Link to="/library" aria-current={pathname.startsWith('/library') || pathname.startsWith('/group') ? 'page' : undefined}><Library size={19} /><span>Library</span></Link>
    <Link to="/settings" aria-current={pathname === '/settings' ? 'page' : undefined}><Settings2 size={19} /><span>You</span></Link>
  </nav>
}

function HomePage({ books, features, scan, error, onOpen, onPlay, onLibrary, onRefresh }: { books: Book[]; features: Features; scan: ScanStatus | null; error: string; onOpen: (book: Book) => void; onPlay: (book: Book) => void; onLibrary: () => void; onRefresh: () => void }) {
  const listening = books.filter(book => (book.progress?.position ?? 0) > 0).slice(0, 5)
  return <section className="home-page">
    <div className="home-greeting"><div><span className="eyebrow">YOUR PERSONAL LIBRARY</span><h1>Good stories,<br />good company.</h1><p>Your next chapter is waiting.</p></div><Avatar className="home-avatar">A</Avatar></div>
    {scan && <ScanProgress scan={scan} onRefresh={onRefresh} />}
    {error && <div className="empty-state">{error} <a href="/auth/google">Sign in</a></div>}
    <SectionHeader title="Continue listening" action="View library" onAction={onLibrary} />
    {listening.length ? <div className="home-book-shelf">{listening.map(book => <button className="home-book-tile" key={book.id} onClick={() => onPlay(book)}><Cover className="home-book-cover" src={book.cover || '/icon.svg'} alt={`${book.title} cover`} /><span className="home-book-copy"><strong>{book.title}</strong><small>{book.artist || 'Audiobook'}</small><Progress value={book.tracks.find(track => track.id === book.progress?.track_id)?.duration ? (book.progress!.position / book.tracks.find(track => track.id === book.progress?.track_id)!.duration) * 100 : 0} label={`${book.title} listening progress`} /></span></button>)}</div> : <div className="home-empty"><Headphones size={24} /><p>Your listening shelf will appear here.</p><Button onClick={onLibrary}>Browse library</Button></div>}
    <SectionHeader title="Your library" subtitle={`${books.length} audiobooks`} action="Browse all" onAction={onLibrary} />
    {books.length ? <div className="home-book-grid">{books.slice(0, 8).map(book => <button className="home-library-tile" key={book.id} onClick={() => onOpen(book)}><Cover className="home-library-cover" src={book.cover || '/icon.svg'} alt={`${book.title} cover`} /><strong>{book.title}</strong><small>{book.artist || 'Audiobook'}</small></button>)}</div> : <div className="home-empty"><p>Your Drive library is ready when you are.</p><Button onClick={onLibrary}>Open library</Button></div>}
    {features.playlists.length > 0 && <><SectionHeader title="Playlists" action="Manage" onAction={onLibrary} /><div className="home-playlists">{features.playlists.slice(0, 4).map((playlist, index) => <button key={playlist.id} onClick={onLibrary}><span className={`playlist-art playlist-art-${index % 4}`}><Headphones size={22} /></span><span><strong>{playlist.name}</strong><small>{playlist.book_ids.length} audiobooks</small></span></button>)}</div></>}
  </section>
}

function SearchPage({ books, query, setQuery, onOpen }: { books: Book[]; query: string; setQuery: (value: string) => void; onOpen: (book: Book) => void }) {
  return <section className="search-page"><span className="eyebrow">FIND YOUR NEXT LISTEN</span><h1>Search</h1><label className="search-box"><Search size={19} /><Input autoFocus placeholder="Search audiobooks, authors, tags…" value={query} onChange={event => setQuery(event.target.value)} /><button type="button" aria-label="Clear search" onClick={() => setQuery('')} disabled={!query}>×</button></label><p className="search-results-count">{query ? `${books.length} matching audiobooks` : 'Search your collection by title, author, or tag.'}</p>{query && <div className="media-grid">{books.map((book, index) => <MediaCard key={book.id} book={book} index={index} favorite={false} rating={0} tags={[]} playlists={[]} onFavorite={() => {}} onRate={() => {}} onTag={() => {}} onAddToPlaylist={() => {}} onOpen={() => onOpen(book)} onPlay={() => onOpen(book)} />)}</div>}{query && !books.length && <div className="empty-state">No audiobooks match “{query}”.</div>}</section>
}

function SectionHeader({ title, subtitle, action, onAction }: { title: string; subtitle?: string; action?: string; onAction?: () => void }) {
  return <div className="section-heading"><div><h2>{title}</h2>{subtitle && <p>{subtitle}</p>}</div>{action && <button className="section-action" onClick={onAction}>{action}<ArrowLeft className="section-action-arrow" size={15} /></button>}</div>
}

function LibraryPage({ groupMode, setGroupMode, onOpenGroup, books, allBooks, features, storage, featureError, onFeature, favoritesOnly, setFavoritesOnly, playlistView, onPlaylistView, reloadFeatures, onCreatePlaylist, onAddToPlaylist, query, setQuery, statusFilter, setStatusFilter, sortBy, setSortBy, error, loading, scan, onRefresh, onOpen, onPlay }: { groupMode: 'artist' | 'directory' | 'album'; setGroupMode: (value: 'artist' | 'directory' | 'album') => void; onOpenGroup: (mode: 'artist' | 'directory' | 'album', name: string) => void; books: Book[]; allBooks: Book[]; features: Features; storage: StorageInfo | null; featureError: string; onFeature: (bookId: string, kind: 'favorite' | 'rating' | 'tags', value: boolean | number | string[]) => void; favoritesOnly: boolean; setFavoritesOnly: (value: boolean) => void; playlistView: string | null; onPlaylistView: (value: string | null) => void; reloadFeatures: () => void; onCreatePlaylist: () => void; onAddToPlaylist: (playlistId: string, bookId: string) => void; query: string; setQuery: (value: string) => void; statusFilter: 'all' | 'in-progress' | 'not-started' | 'completed'; setStatusFilter: (value: 'all' | 'in-progress' | 'not-started' | 'completed') => void; sortBy: 'title' | 'author' | 'duration' | 'rating'; setSortBy: (value: 'title' | 'author' | 'duration' | 'rating') => void; error: string; loading: boolean; scan: ScanStatus | null; onRefresh: () => void; onOpen: (book: Book) => void; onPlay: (book: Book) => void }) {
  return <section className="library-page">
    <div className="hero"><div><div className="eyebrow"><BookOpen size={14} /> YOUR COLLECTION</div><h1>Your library</h1><p>Stories for wherever the day takes you.</p></div><div className="hero-art"><Headphones size={88} strokeWidth={1.1} /></div></div>
    <div className="library-heading"><div className="library-heading-copy"><h2>All audiobooks</h2><span>{books.length.toLocaleString()} titles</span></div><label className="search-box"><Search size={17} /><Input placeholder="Search your library" value={query} onChange={event => setQuery(event.target.value)} /></label></div>
    <div className="group-toolbar" role="group" aria-label="Browse audiobooks by"><span className="browse-label">Browse by</span>{(['artist', 'directory', 'album'] as const).map(mode => <button key={mode} className={groupMode === mode ? 'active' : ''} aria-pressed={groupMode === mode} onClick={() => setGroupMode(mode)}>{mode[0].toUpperCase() + mode.slice(1)}</button>)}</div>
    <details className="filter-panel"><summary><SlidersHorizontal size={16} /><span>Filters and sort</span><span className="filter-summary-state">{statusFilter !== 'all' || favoritesOnly || sortBy !== 'title' ? 'Applied' : 'Optional'}</span></summary><div className="filter-panel-content"><div className="filter-options" role="group" aria-label="Filter audiobooks">{(['all', 'in-progress', 'not-started', 'completed'] as const).map(value => <button key={value} className={`filter-chip ${statusFilter === value ? 'active' : ''}`} aria-pressed={statusFilter === value} onClick={() => setStatusFilter(value)}>{value === 'all' ? 'All books' : value === 'in-progress' ? 'In progress' : value === 'not-started' ? 'Not started' : 'Finished'}</button>)}<button className={`filter-chip ${favoritesOnly ? 'active' : ''}`} aria-pressed={favoritesOnly} onClick={() => setFavoritesOnly(!favoritesOnly)}>Favorites</button></div><label className="sort-control"><span>Sort by</span><select value={sortBy} onChange={event => setSortBy(event.target.value as typeof sortBy)} aria-label="Sort audiobooks"><option value="title">Title</option><option value="author">Author</option><option value="duration">Longest</option><option value="rating">Top rated</option></select></label></div></details>
    {featureError && <div className="empty-state">{featureError}</div>}
    {books.some(book => (book.progress?.position ?? 0) > 0) && <><div className="tracks-heading"><div><h2>Continue listening</h2><p>Your saved place</p></div></div><div className="resume-shelf">{books.filter(book => (book.progress?.position ?? 0) > 0).slice(0, 5).map(book => { const track = book.tracks.find(item => item.id === book.progress?.track_id); return track ? <button key={book.id} onClick={() => onPlay(book)}><Cover src={book.cover || '/icon.svg'} alt="" /><span><strong>{book.title}</strong><small>{track.title || track.name} · {duration(book.progress?.position ?? 0)} listened</small></span></button> : null })}</div></>}
    {storage && <p className="storage-summary">{storage.tracks.toLocaleString()} files · {formatBytes(storage.bytes)} · {storage.formats.map(item => `${item.mime_type.replace('audio/', '').replace('application/', '')} ${item.count}`).join(' · ')}</p>}
    {features.playlists.length > 0 && <div className="playlist-shelf"><strong>Playlists</strong><button className={!playlistView ? 'active' : ''} onClick={() => onPlaylistView(null)}>All books</button>{features.playlists.map(playlist => <span key={playlist.id}><button className={playlistView === playlist.id ? 'active' : ''} onClick={() => onPlaylistView(playlist.id)}>{playlist.name} · {playlist.book_ids.length}</button><button aria-label={`Delete ${playlist.name}`} onClick={() => { if (window.confirm(`Delete playlist “${playlist.name}”?`)) void api(`/api/playlists/${playlist.id}`, { method: 'DELETE' }).then(reloadFeatures) }}>×</button></span>)}<button onClick={onCreatePlaylist}><Plus size={14} /> New playlist</button></div>}
    {!features.playlists.length && <button className="create-playlist" onClick={onCreatePlaylist}><Plus size={14} /> Create playlist</button>}
    {features.history.length > 0 && <><div className="tracks-heading"><div><h2>Recently played</h2><p>Pick up where you left off</p></div></div><div className="media-grid recent-grid">{features.history.slice(0, 4).map((item, index) => { const book = allBooks.find(entry => entry.id === item.book_id); return book ? <MediaCard key={`${item.book_id}-${item.played_at}`} book={book} index={index} favorite={features.favorites.includes(book.id)} rating={features.ratings[book.id] ?? 0} tags={features.tags[book.id] ?? []} playlists={features.playlists} onFavorite={() => onFeature(book.id, 'favorite', !features.favorites.includes(book.id))} onRate={rating => onFeature(book.id, 'rating', rating)} onTag={() => { const tag = window.prompt('Add a tag'); if (tag?.trim()) onFeature(book.id, 'tags', [...(features.tags[book.id] ?? []), tag.trim()]) }} onAddToPlaylist={onAddToPlaylist} onOpen={() => onOpen(book)} onPlay={() => onPlay(book)} /> : null })}</div></>}
    {!loading && !error && <div className="results-count" aria-live="polite">Showing {books.length} {books.length === 1 ? 'audiobook' : 'audiobooks'}</div>}
    {scan && <ScanProgress scan={scan} onRefresh={onRefresh} />}
    {error && <div className="empty-state">{error} <a href="/auth/google">Sign in</a></div>}
    {loading ? <LibrarySkeleton /> : !error && <GroupGrid books={books} mode={groupMode} onOpenGroup={onOpenGroup} />}
    {!loading && !error && books.length === 0 && <div className="empty-state">{query || statusFilter !== 'all' ? 'No audiobooks match these filters.' : 'No audiobooks found. Refresh after you sign in.'}</div>}
    {features.history.length > 0 && <div className="tracks-heading"><div><h2>Listening history</h2><p>Recent activity</p></div></div>}
    {features.history.length > 0 && <div className="history-list">{features.history.slice(0, 10).map((item, index) => { const book = allBooks.find(entry => entry.id === item.book_id); const track = book?.tracks.find(entry => entry.id === item.track_id); return book && track ? <button key={`${item.book_id}-${item.played_at}-${index}`} onClick={() => onPlay(book)}><span>{book.title}</span><small>{track.title || track.name} · {new Date(item.played_at + 'Z').toLocaleString()}</small></button> : null })}</div>}
  </section>
}

function GroupGrid({ books, mode, onOpenGroup }: { books: Book[]; mode: 'artist' | 'directory' | 'album'; onOpenGroup: (mode: 'artist' | 'directory' | 'album', name: string) => void }) {
  if (mode === 'album') return <div className="media-grid">{books.map(book => <button className="group-card" key={book.id} onClick={() => onOpenGroup('album', book.album?.trim() || book.title)}><Cover className="media-cover" src={book.cover || '/icon.svg'} alt="" /><strong>{book.album?.trim() || book.title}</strong><span>{book.artist || 'Unknown artist'}</span></button>)}</div>
  const groups = new Map<string, Book[]>()
  for (const book of books) {
    const path = book.directory || book.tracks[0]?.path || book.tracks[0]?.name || ''
    const key = mode === 'artist' ? book.artist?.trim() || 'Unknown artist' : path.split('/')[0] || 'Root'
    groups.set(key, [...(groups.get(key) ?? []), book])
  }
  const sorted = [...groups.entries()].sort(([a], [b]) => a.localeCompare(b))
  return sorted.length ? <div className="media-grid">{sorted.map(([name, members]) => <button className="group-card" key={name} onClick={() => onOpenGroup(mode, name)}><Cover className="media-cover" src={members[0].cover || '/icon.svg'} alt="" /><strong>{name}</strong><span>{members.length} {members.length === 1 ? 'album' : 'albums'}</span></button>)}</div> : <div className="empty-state">No groups found.</div>
}

function GroupPage({ books, onOpen, onOpenGroup }: { books: Book[]; onOpen: (book: Book) => void; onOpenGroup: (mode: 'artist' | 'directory' | 'album', name: string) => void }) {
  const { mode = '' } = useParams()
  const [params] = useSearchParams()
  const name = params.get('name') || ''
  const groupMode = mode === 'artist' || mode === 'directory' || mode === 'album' ? mode : 'artist'
  const members = books.filter(book => groupMode === 'artist' ? (book.artist?.trim() || 'Unknown artist') === name : groupMode === 'album' ? (book.album?.trim() || book.title) === name : (book.directory || book.tracks[0]?.path || book.tracks[0]?.name || '').startsWith(name ? `${name}/` : ''))
  const children = groupMode === 'directory' ? [...new Set(members.map(book => (book.directory || '').slice(name ? name.length + 1 : 0)).filter(path => path.includes('/')).map(path => path.split('/')[0]))] : groupMode === 'artist' ? [...new Set(members.map(book => book.album?.trim()).filter((album): album is string => Boolean(album)))] : []
  const albums = groupMode === 'directory' ? members.filter(book => !(book.directory || '').slice(name ? name.length + 1 : 0).includes('/')) : groupMode === 'artist' && children.length ? members.filter(book => book.album?.trim() === name) : members
  const title = name || (groupMode === 'artist' ? 'Artists' : groupMode === 'album' ? 'Albums' : 'Directories')
  return <section className="library-page"><Link className="back-link" to="/"><ArrowLeft size={17} /> Your library</Link><div className="tracks-heading"><div><div className="eyebrow">{groupMode === 'artist' ? 'ARTIST' : groupMode === 'album' ? 'ALBUM' : 'DIRECTORY'}</div><h1>{title}</h1><p>{albums.length} {albums.length === 1 ? 'album' : 'albums'}{children.length ? ` · ${children.length} folders` : ''}</p></div></div>{children.length > 0 && <div className="media-grid">{children.map(child => { const path = groupMode === 'directory' ? name ? `${name}/${child}` : child : child; const cover = members.find(book => groupMode === 'artist' ? book.album === child : book.directory === path)?.cover; return <button className="group-card" key={path} onClick={() => onOpenGroup(groupMode === 'artist' ? 'album' : 'directory', path)}><Cover className="media-cover" src={cover || '/icon.svg'} alt="" /><strong>{child}</strong><span>{groupMode === 'artist' ? 'Album' : 'Folder'}</span></button> })}</div>}<div className="media-grid">{albums.map(book => <button className="group-card" key={book.id} onClick={() => onOpen(book)}><Cover className="media-cover" src={book.cover || '/icon.svg'} alt={`${book.title} cover`} /><strong>{book.title}</strong><span>{book.artist || book.directory || 'Audiobook'}</span></button>)}</div>{members.length === 0 && <div className="empty-state">No audiobooks found in this group.</div>}</section>
}

function SettingsPage({ reloadLibrary }: { reloadLibrary: () => void }) {
  const [folders, setFolders] = useState<{ id: string; name: string; path: string }[]>([])
  const [selected, setSelected] = useState<string[]>([])
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [saved, setSaved] = useState(false)
  useEffect(() => {
    void Promise.all([api<{ excluded_directories: typeof folders }>('/api/settings/excluded-directories'), api<typeof folders>('/api/directories')])
      .then(([settings, list]) => { setFolders(list); setSelected(settings.excluded_directories.map(item => item.path)) })
      .catch(e => setError(e instanceof Error ? e.message : 'Could not load Drive folders.'))
      .finally(() => setLoading(false))
  }, [])
  const save = async () => {
    setSaving(true); setError(''); setSaved(false)
    try {
      await api('/api/settings/excluded-directories', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ paths: selected }) })
      setSaved(true); reloadLibrary()
    } catch (e) { setError(e instanceof Error ? e.message : 'Could not save exclusions.') }
    finally { setSaving(false) }
  }
  return <section className="library-page settings-page"><div className="eyebrow"><Settings2 size={14} /> PREFERENCES</div><h1>Scan settings</h1><p className="settings-intro">Choose Drive folders to skip. Saving removes matching audio from your library and saved progress.</p>
    {loading ? <LibrarySkeleton /> : <Card className="folder-picker"><h2>Google Drive folders</h2><p>Folders found in the scanned library. Select folders to exclude. Drive listing is not required.</p>{folders.length ? folders.map(folder => <label className="folder-option" key={folder.id}><input type="checkbox" checked={selected.includes(folder.path)} onChange={event => setSelected(current => event.target.checked ? [...current, folder.path] : current.filter(path => path !== folder.path))} /><span>{folder.path}</span></label>) : <p>No subfolders found.</p>}</Card>}
    {error && <p className="settings-error" role="alert">{error}</p>}{saved && <p className="settings-saved" role="status">Exclusions saved. Start a scan to refresh the library.</p>}
    <Button disabled={loading || saving} onClick={() => void save()}>{saving ? 'Saving…' : 'Save exclusions'}</Button>
  </section>
}

function ScanProgress({ scan, onRefresh }: { scan: ScanStatus; onRefresh: () => void }) {
  if (scan.status === 'idle') return null
  const running = scan.status === 'running'
  const done = scan.status === 'completed'
  const percent = scan.total ? Math.min(100, Math.round(scan.processed / scan.total * 100)) : 0
  return <Card className={`scan-card ${running ? 'scan-running' : done ? 'scan-done' : 'scan-error'}`} role="status" aria-live="polite">
    <div className="scan-icon">{running ? <LoaderCircle className="spin" size={18} /> : <CheckCircle2 size={18} />}</div>
    <div className="scan-content"><div className="scan-topline"><strong>{running ? 'Updating your library' : done ? 'Library is up to date' : 'Scan needs attention'}</strong><span>{scan.total ? `${percent}%` : running ? 'Preparing' : scan.status}</span></div>
      <div className="scan-bar"><span className={scan.total ? '' : 'indeterminate'} style={scan.total ? { width: `${percent}%` } : undefined} /></div>
      <div className="scan-subline"><span>{scan.error || (scan.total ? `${scan.processed.toLocaleString()} of ${scan.total.toLocaleString()} files checked` : scan.current || 'Connecting to Google Drive')}</span>{running && scan.total > 0 && <span>{Math.max(0, scan.total - scan.processed).toLocaleString()} left</span>}</div>
      {running && scan.current && scan.total > 0 && <div className="scan-current" title={scan.current}>{scan.current}</div>}
      {!running && !done && <button className="scan-retry" onClick={onRefresh}>Try again</button>}
    </div>
  </Card>
}

function LibrarySkeleton() {
  return <div className="media-grid" aria-label="Loading your library" aria-busy="true">{Array.from({ length: 12 }, (_, index) => <Card className="media-card media-skeleton" key={index}><Skeleton className="skeleton-cover" /><Skeleton className="skeleton-line" /><Skeleton className="skeleton-line short" /></Card>)}</div>
}

function BookSkeleton() {
  return <div className="book-skeleton" aria-label="Loading audiobook" aria-busy="true"><Skeleton className="skeleton-book-cover" /><div className="book-skeleton-lines"><Skeleton className="skeleton-line short" /><Skeleton className="skeleton-line title-line" /><Skeleton className="skeleton-line" /><Skeleton className="skeleton-action" /></div><Skeleton className="skeleton-track" /><Skeleton className="skeleton-track" /></div>
}

function MediaCard({ book, index, favorite, rating, tags, playlists, onFavorite, onRate, onTag, onAddToPlaylist, onOpen, onPlay }: { book: Book; index: number; favorite: boolean; rating: number; tags: string[]; playlists: Features['playlists']; onFavorite: () => void; onRate: (rating: number) => void; onTag: () => void; onAddToPlaylist: (playlistId: string, bookId: string) => void; onOpen: () => void; onPlay: () => void }) {
  const track = book.tracks.find(item => item.id === book.progress?.track_id)
  const percent = track?.duration ? Math.min(100, Math.round(book.progress!.position / track.duration * 100)) : 0
  return <Card className="media-card media-card-enter" style={{ '--card-index': Math.min(index, 12) } as CSSProperties}><button className="cover-action" onClick={onOpen} aria-label={`Open ${book.title}`}><Cover className="media-cover" src={book.cover || '/icon.svg'} alt={`${book.title} cover`} /><span className="hover-play" onClick={event => { event.stopPropagation(); onPlay() }}><PlayIcon /></span></button>
    <button className="favorite-toggle" aria-label={favorite ? 'Remove favorite' : 'Add favorite'} aria-pressed={favorite} onClick={onFavorite}><Heart size={16} fill={favorite ? 'currentColor' : 'none'} /></button>
    <button className="media-title" onClick={onOpen}>{book.title}</button><div className="media-artist">{book.artist || 'Audiobook'}</div>
    <div className="media-meta"><span>{book.tracks.length} {book.tracks.length === 1 ? 'part' : 'parts'}</span><span>{duration(book.duration)}</span></div>
    {rating > 0 && <div className="book-rating" aria-label={`Rating: ${rating} out of 5`}>{Array.from({ length: rating }, (_, i) => <Star size={12} fill="currentColor" key={i} />)}</div>}
    {tags.length > 0 && <div className="book-tags">{tags.map(tag => <span key={tag}>{tag}</span>)}</div>}
    {book.progress && <div className="resume-indicator">Continue · {Math.floor(book.progress.position / 60)} min</div>}
    {book.progress && <div className="book-progress-bar"><span style={{ width: `${percent}%` }} /></div>}
    <div className="card-tools"><button onClick={onTag}>Tag</button><select aria-label={`Rate ${book.title}`} value={rating} onChange={event => onRate(Number(event.target.value))}><option value={0}>Rate</option>{[1, 2, 3, 4, 5].map(value => <option key={value} value={value}>{value} star{value === 1 ? '' : 's'}</option>)}</select>{playlists.length > 0 && <select aria-label={`Add ${book.title} to playlist`} value="" onChange={event => { if (event.target.value) onAddToPlaylist(event.target.value, book.id) }}><option value="">Add to…</option>{playlists.map(playlist => <option key={playlist.id} value={playlist.id}>{playlist.name}</option>)}</select>}</div>
  </Card>
}

function BookPage({ onPlay }: { onPlay: (book: Book, track: Track, start?: number) => void }) {
  const { bookId = '' } = useParams()
  const [book, setBook] = useState<Book | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  useEffect(() => {
    setLoading(true)
    setBook(null)
    setError('')
    void api<Book>(`/api/books/${encodeURIComponent(bookId)}`).then(setBook).catch(e => setError(e instanceof Error ? e.message : 'Could not load book.')).finally(() => setLoading(false))
  }, [bookId])
  if (error) return <div className="empty-state">{error}</div>
  if (loading) return <BookSkeleton />
  if (!book) return <div className="empty-state">Audiobook not found.</div>
  const totalChapters = book.tracks.reduce((sum, track) => sum + (track.chapter_count ?? 0), 0)
  return <section className="book-page"><Link className="back-link" to="/"><ArrowLeft size={17} /> Your library</Link>
    <div className="book-hero"><Cover className="book-cover" src={book.cover || '/icon.svg'} alt={`${book.title} cover`} /><div className="book-info"><div className="eyebrow">AUDIOBOOK</div><h1>{book.title}</h1><p className="book-author">{book.artist || 'Unknown author'}</p><div className="book-facts"><span><Headphones size={15} />{book.tracks.length} parts</span><span><Clock3 size={15} />{duration(book.duration)}</span><span><BookOpen size={15} />{totalChapters} chapters</span></div><Button className="primary-action" onClick={() => { const track = book.tracks.find(item => item.id === book.progress?.track_id) || book.tracks[0]; if (track) onPlay(book, track, book.progress?.position || 0) }}><PlayIcon /> Listen now</Button></div></div>
    <div className="tracks-heading"><div><h2>Contents</h2><p>{book.tracks.length} {book.tracks.length === 1 ? 'audio file' : 'audio files'}</p></div></div>
    <div className="track-list">{book.tracks.map((track, index) => <TrackSection key={track.id} book={book} track={track} index={index} onPlay={onPlay} />)}</div>
  </section>
}

function TrackSection({ book, track, index, onPlay }: { book: Book; track: Track; index: number; onPlay: (book: Book, track: Track, start?: number) => void }) {
  const [chapters, setChapters] = useState<Chapter[]>([])
  const [offset, setOffset] = useState(0)
  const [hasMore, setHasMore] = useState(true)
  const [loading, setLoading] = useState(false)
  const [chapterError, setChapterError] = useState(false)
  const sentinel = useRef<HTMLDivElement>(null)
  const loadMore = useCallback(async () => {
    if (loading || !hasMore) return
    setChapterError(false)
    setLoading(true)
    try {
      const page = await api<{ chapters: Chapter[]; next_offset: number | null }>(`/api/tracks/${encodeURIComponent(track.id)}/chapters?offset=${offset}&limit=50`)
      setChapters(previous => [...previous, ...page.chapters])
      setOffset(page.next_offset ?? offset + page.chapters.length)
      setHasMore(page.next_offset !== null)
    } catch { setChapterError(true) }
    finally { setLoading(false) }
  }, [track.id, offset, loading, hasMore])
  useEffect(() => { setChapters([]); setOffset(0); setHasMore(true) }, [track.id])
  useEffect(() => {
    const target = sentinel.current
    if (!target || !hasMore) return
    const observer = new IntersectionObserver(entries => { if (entries.some(entry => entry.isIntersecting)) void loadMore() }, { rootMargin: '300px' })
    observer.observe(target)
    return () => observer.disconnect()
  }, [loadMore, hasMore])
  return <Card className="track-section"><div className="track-header"><span className="track-index">{String(index + 1).padStart(2, '0')}</span><div className="track-name"><strong>{track.title || track.name}</strong><span>{duration(track.duration)} · {track.chapter_count ?? 0} chapters</span></div><Button className="track-play" aria-label={`Play ${track.title}`} onClick={() => onPlay(book, track)}><PlayIcon /></Button></div>
    <div className="chapter-list">{chapters.map((chapter, i) => <button className="chapter-row" key={`${track.id}-${chapter.number ?? i}-${chapter.start}`} onClick={() => onPlay(book, track, chapter.start)}><span className="chapter-number">{String(chapter.number ?? i + 1).padStart(2, '0')}</span><span className="chapter-title">{chapter.title || `Chapter ${chapter.number ?? i + 1}`}</span><span className="chapter-time">{timestamp(chapter.start)}</span><PlayIcon /></button>)}</div>
    {chapterError && <button className="load-more" onClick={() => void loadMore()}>Could not load chapters. Retry</button>}
    {loading && <div className="chapter-loading" aria-label="Loading chapters" aria-busy="true">{Array.from({ length: 3 }, (_, i) => <Skeleton className="chapter-skeleton" key={i} />)}</div>}
    {hasMore && <div className="chapter-sentinel" ref={sentinel} aria-hidden="true" />}
  </Card>
}

function formatBytes(bytes: number) { return bytes >= 1024 ** 3 ? `${(bytes / 1024 ** 3).toFixed(1)} GB` : bytes >= 1024 ** 2 ? `${(bytes / 1024 ** 2).toFixed(1)} MB` : `${(bytes / 1024).toFixed(0)} KB` }
function duration(seconds: number) { const mins = Math.floor((seconds || 0) / 60); const hrs = Math.floor(mins / 60); return hrs ? `${hrs} hr ${mins % 60} min` : `${mins} min` }
function timestamp(seconds: number) { const mins = Math.floor(seconds / 60); return `${mins}:${String(Math.floor(seconds % 60)).padStart(2, '0')}` }

export default App
