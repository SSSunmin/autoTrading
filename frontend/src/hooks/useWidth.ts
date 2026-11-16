import { useLayoutEffect, useRef, useState } from 'react'

/** An element's rendered width in px, kept up to date as it resizes. A chart drawn on that width (viewBox = its px)
 *  keeps its text at the CSS size: on a fixed 1000-wide viewBox the browser scaled 12px axis labels down to 5–8px
 *  (type-scale). `fallback` until it is measured (and where there is no layout: tests). */
export function useWidth<T extends Element>(fallback: number) {
  const ref = useRef<T>(null)
  const [width, setWidth] = useState(fallback)
  useLayoutEffect(() => {
    const el = ref.current
    if (!el || typeof ResizeObserver === 'undefined') return
    const read = () => { const w = el.getBoundingClientRect().width; if (w > 0) setWidth(w) }
    read()
    const ro = new ResizeObserver(read)
    ro.observe(el)
    return () => ro.disconnect()
  }, [])
  return [ref, width] as const
}
