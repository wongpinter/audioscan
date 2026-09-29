import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from 'react'
import { Link, Route, Routes, useNavigate, useParams } from 'react-router-dom'
import { ArrowLeft, BookOpen, CheckCircle2, Clock3, Headphones, Library, LoaderCircle, Search, Settings2, SlidersHorizontal, Heart, Star, Plus } from 'lucide-react'
import { Button, Card, Cover, Input, PlayIcon, Skeleton } from './components/ui'
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
  const navigate = useNavigate()

  const load = useCallback(async () => {
    try {
      const result = await api<Book[]>('/api/library')
      setBooks(result)
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
    void fetch(`/api/history/${encodeURIComponent(book.id)}`, { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ track_id: track.id }) })
    setActiveBook(book); setActiveTrack(track); setStartAt(start)
    void loadFeatures()
  }
  const moveTrack = (delta: number) => {
    if (!activeBook || !activeTrack) return
    const index = activeBook.tracks.findIndex(track => track.id === activeTrack.id)
    const next = activeBook.tracks[index + delta]
    if (next) play(activeBook, next)
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
  const saveProgress = useCallback((time: number) => {
    if (activeBook && activeTrack && time > 0) void fetch(`/api/progress/${encodeURIComponent(activeBook.id)}`, {
      method: 'PUT', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ track_id: activeTrack.id, position: time }),
    })
  }, [activeBook, activeTrack])

  return <div className="app-shell">
    <aside className="sidebar">
      <Link className="brand" to="/"><span className="brand-mark"><Headphones size={20} /></span><span>audio<span className="brand-accent">scan</span></span></Link>
      <div className="nav-label">LIBRARY</div><Link className="nav-item selected" to="/"><Library size={18} />Your library</Link>
      <div className="sidebar-footer"><span className="avatar">A</span><span><strong>Personal library</strong><small>Google Drive</small></span><Settings2 size={17} /></div>
    </aside>
    <main className="main-content">
      <header className="topbar"><div className="breadcrumbs"><span>Library</span><span className="crumb-separator">/</span><span className="crumb-current">Audiobooks</span></div><Button className="top-action" disabled={scan?.status === 'running'} onClick={() => void refreshLibrary()}>{scan?.status === 'running' ? 'Scanning…' : 'Scan library'}</Button></header>
      <Routes>
        <Route path="/" element={<LibraryPage books={filtered} allBooks={books} features={features} storage={storage} featureError={featureError} onFeature={updateBookFeature} favoritesOnly={favoritesOnly} setFavoritesOnly={setFavoritesOnly} onCreatePlaylist={makePlaylist} onAddToPlaylist={addToPlaylist} onPlaylistView={setPlaylistView} playlistView={playlistView} reloadFeatures={loadFeatures} query={filter} setQuery={setFilter} statusFilter={statusFilter} setStatusFilter={setStatusFilter} sortBy={sortBy} setSortBy={setSortBy} error={error} loading={loading} scan={scan} onRefresh={() => void refreshLibrary()} onOpen={book => navigate(`/book/${encodeURIComponent(book.id)}`)} onPlay={book => { const track = book.tracks[0]; if (track) play(book, track, book.progress?.track_id === track.id ? book.progress.position : 0) }} />} />
        <Route path="/book/:bookId" element={<BookPage onPlay={play} />} />
        <Route path="*" element={<div className="empty-state">Page not found.</div>} />
      </Routes>
    </main>
    <PlayerBar book={activeBook} track={activeTrack} start={startAt} onNext={() => moveTrack(1)} onPrevious={() => moveTrack(-1)} onTime={saveProgress} />
  </div>
}

function LibraryPage({ books, allBooks, features, storage, featureError, onFeature, favoritesOnly, setFavoritesOnly, playlistView, onPlaylistView, reloadFeatures, onCreatePlaylist, onAddToPlaylist, query, setQuery, statusFilter, setStatusFilter, sortBy, setSortBy, error, loading, scan, onRefresh, onOpen, onPlay }: { books: Book[]; allBooks: Book[]; features: Features; storage: StorageInfo | null; featureError: string; onFeature: (bookId: string, kind: 'favorite' | 'rating' | 'tags', value: boolean | number | string[]) => void; favoritesOnly: boolean; setFavoritesOnly: (value: boolean) => void; playlistView: string | null; onPlaylistView: (value: string | null) => void; reloadFeatures: () => void; onCreatePlaylist: () => void; onAddToPlaylist: (playlistId: string, bookId: string) => void; query: string; setQuery: (value: string) => void; statusFilter: 'all' | 'in-progress' | 'not-started' | 'completed'; setStatusFilter: (value: 'all' | 'in-progress' | 'not-started' | 'completed') => void; sortBy: 'title' | 'author' | 'duration' | 'rating'; setSortBy: (value: 'title' | 'author' | 'duration' | 'rating') => void; error: string; loading: boolean; scan: ScanStatus | null; onRefresh: () => void; onOpen: (book: Book) => void; onPlay: (book: Book) => void }) {
  return <section className="library-page">
    <div className="hero"><div><div className="eyebrow"><BookOpen size={14} /> YOUR COLLECTION</div><h1>Your library</h1><p>Stories for wherever the day takes you.</p></div><div className="hero-art"><Headphones size={88} strokeWidth={1.1} /></div></div>
    <div className="section-heading"><div><h2>All audiobooks</h2><p>{books.length} {books.length === 1 ? 'title' : 'titles'} in your collection</p></div><label className="search-box"><Search size={17} /><Input placeholder="Search your library" value={query} onChange={event => setQuery(event.target.value)} /></label></div>
    <div className="filter-toolbar"><div className="filter-options" role="group" aria-label="Filter audiobooks"><SlidersHorizontal size={15} className="filter-icon" />{(['all', 'in-progress', 'not-started', 'completed'] as const).map(value => <button key={value} className={`filter-chip ${statusFilter === value ? 'active' : ''}`} aria-pressed={statusFilter === value} onClick={() => setStatusFilter(value)}>{value === 'all' ? 'All books' : value === 'in-progress' ? 'In progress' : value === 'not-started' ? 'Not started' : 'Finished'}</button>)}<button className={`filter-chip ${favoritesOnly ? 'active' : ''}`} aria-pressed={favoritesOnly} onClick={() => setFavoritesOnly(!favoritesOnly)}>Favorites</button></div><label className="sort-control"><span>Sort</span><select value={sortBy} onChange={event => setSortBy(event.target.value as typeof sortBy)} aria-label="Sort audiobooks"><option value="title">Title</option><option value="author">Author</option><option value="duration">Longest</option><option value="rating">Top rated</option></select></label></div>
    {featureError && <div className="empty-state">{featureError}</div>}
    {books.some(book => (book.progress?.position ?? 0) > 0) && <><div className="tracks-heading"><div><h2>Continue listening</h2><p>Your saved place</p></div></div><div className="resume-shelf">{books.filter(book => (book.progress?.position ?? 0) > 0).slice(0, 5).map(book => { const track = book.tracks.find(item => item.id === book.progress?.track_id); return track ? <button key={book.id} onClick={() => onPlay(book)}><Cover src={book.cover || '/icon.svg'} alt="" /><span><strong>{book.title}</strong><small>{track.title || track.name} · {duration(book.progress?.position ?? 0)} listened</small></span></button> : null })}</div></>}
    {storage && <p className="storage-summary">{storage.tracks.toLocaleString()} files · {formatBytes(storage.bytes)} · {storage.formats.map(item => `${item.mime_type.replace('audio/', '').replace('application/', '')} ${item.count}`).join(' · ')}</p>}
    {features.playlists.length > 0 && <div className="playlist-shelf"><strong>Playlists</strong><button className={!playlistView ? 'active' : ''} onClick={() => onPlaylistView(null)}>All books</button>{features.playlists.map(playlist => <span key={playlist.id}><button className={playlistView === playlist.id ? 'active' : ''} onClick={() => onPlaylistView(playlist.id)}>{playlist.name} · {playlist.book_ids.length}</button><button aria-label={`Delete ${playlist.name}`} onClick={() => { if (window.confirm(`Delete playlist “${playlist.name}”?`)) void api(`/api/playlists/${playlist.id}`, { method: 'DELETE' }).then(reloadFeatures) }}>×</button></span>)}<button onClick={onCreatePlaylist}><Plus size={14} /> New playlist</button></div>}
    {!features.playlists.length && <button className="create-playlist" onClick={onCreatePlaylist}><Plus size={14} /> Create playlist</button>}
    {features.history.length > 0 && <><div className="tracks-heading"><div><h2>Recently played</h2><p>Pick up where you left off</p></div></div><div className="media-grid recent-grid">{features.history.slice(0, 4).map((item, index) => { const book = allBooks.find(entry => entry.id === item.book_id); return book ? <MediaCard key={`${item.book_id}-${item.played_at}`} book={book} index={index} favorite={features.favorites.includes(book.id)} rating={features.ratings[book.id] ?? 0} tags={features.tags[book.id] ?? []} playlists={features.playlists} onFavorite={() => onFeature(book.id, 'favorite', !features.favorites.includes(book.id))} onRate={rating => onFeature(book.id, 'rating', rating)} onTag={() => { const tag = window.prompt('Add a tag'); if (tag?.trim()) onFeature(book.id, 'tags', [...(features.tags[book.id] ?? []), tag.trim()]) }} onAddToPlaylist={onAddToPlaylist} onOpen={() => onOpen(book)} onPlay={() => onPlay(book)} /> : null })}</div></>}
    {!loading && !error && <div className="results-count" aria-live="polite">Showing {books.length} {books.length === 1 ? 'audiobook' : 'audiobooks'}</div>}
    {scan && <ScanProgress scan={scan} onRefresh={onRefresh} />}
    {error && <div className="empty-state">{error} <a href="/auth/google">Sign in</a></div>}
    {loading ? <LibrarySkeleton /> : <div className="media-grid">{books.map((book, index) => <MediaCard key={book.id} book={book} index={index} favorite={features.favorites.includes(book.id)} rating={features.ratings[book.id] ?? 0} tags={features.tags[book.id] ?? []} playlists={features.playlists} onFavorite={() => onFeature(book.id, 'favorite', !features.favorites.includes(book.id))} onRate={rating => onFeature(book.id, 'rating', rating)} onTag={() => { const tag = window.prompt('Add a tag'); if (tag?.trim()) onFeature(book.id, 'tags', [...(features.tags[book.id] ?? []), tag.trim()]) }} onAddToPlaylist={onAddToPlaylist} onOpen={() => onOpen(book)} onPlay={() => onPlay(book)} />)}</div>}
    {!loading && !error && books.length === 0 && <div className="empty-state">{query || statusFilter !== 'all' ? 'No audiobooks match these filters.' : 'No audiobooks found. Refresh after you sign in.'}</div>}
    {features.history.length > 0 && <div className="tracks-heading"><div><h2>Listening history</h2><p>Recent activity</p></div></div>}
    {features.history.length > 0 && <div className="history-list">{features.history.slice(0, 10).map((item, index) => { const book = allBooks.find(entry => entry.id === item.book_id); const track = book?.tracks.find(entry => entry.id === item.track_id); return book && track ? <button key={`${item.book_id}-${item.played_at}-${index}`} onClick={() => onPlay(book)}><span>{book.title}</span><small>{track.title || track.name} · {new Date(item.played_at + 'Z').toLocaleString()}</small></button> : null })}</div>}
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
