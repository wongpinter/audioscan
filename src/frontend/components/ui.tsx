import { useState, type ButtonHTMLAttributes, type HTMLAttributes, type ImgHTMLAttributes } from 'react'
import { Play } from 'lucide-react'

export function Button({ className = '', ...props }: ButtonHTMLAttributes<HTMLButtonElement>) {
  return <button className={`button ${className}`} {...props} />
}

export function IconButton({ className = '', ...props }: ButtonHTMLAttributes<HTMLButtonElement>) {
  return <button className={`icon-button ${className}`} {...props} />
}

export function Input({ className = '', ...props }: React.InputHTMLAttributes<HTMLInputElement>) {
  return <input className={`input ${className}`} {...props} />
}

export function Card({ className = '', ...props }: HTMLAttributes<HTMLElement>) {
  return <article className={`card ${className}`} {...props} />
}

export function Skeleton({ className = '' }: { className?: string }) {
  return <span className={`skeleton ${className}`} aria-hidden="true" />
}

export function Cover({ className = '', alt, onLoad, onError, ...props }: ImgHTMLAttributes<HTMLImageElement>) {
  const [loaded, setLoaded] = useState(false)
  const [failed, setFailed] = useState(false)
  return <span className={`cover-frame ${className}`}>
    {!loaded && !failed && <Skeleton className="cover-skeleton" />}
    {!failed && <img className={`cover ${loaded ? 'cover-loaded' : 'cover-pending'}`} alt={alt ?? ''} loading="lazy" onLoad={event => { setLoaded(true); onLoad?.(event) }} onError={event => { setFailed(true); onError?.(event) }} {...props} />}
    {failed && <span className="cover-fallback"><Play size={20} aria-hidden="true" /></span>}
  </span>
}

export function Spinner({ className = '' }: { className?: string }) {
  return <span className={`spinner ${className}`} role="status" aria-label="Loading" />
}

export function PlayIcon() {
  return <Play size={18} fill="currentColor" aria-hidden="true" />
}
