export type Chapter = { number?: number; title?: string; start: number; end?: number | null }
export type Track = {
  id: string
  book_id: string
  title: string
  name: string
  path?: string
  duration: number
  chapters: Chapter[]
  chapter_count?: number
  format?: string
}
export type Progress = { track_id: string; position: number }
export type Book = {
  id: string
  title: string
  artist?: string | null
  album?: string | null
  directory?: string | null
  cover?: string | null
  duration: number
  tracks: Track[]
  progress?: Progress | null
}
