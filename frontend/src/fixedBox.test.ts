/// <reference types="node" />
import { readdirSync, readFileSync } from 'node:fs'
import { join, relative } from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

/** tile-overflow (2026-11-18): the 결정 이력 date sat in a 2.8rem square and ran 3–7px past it — the second time text
 *  outgrew a fixed box (layout-overflow had the KPI cards). A box with text takes a minimum width and padding; a fixed
 *  `width` / `height` in a length is let through only for what is listed here, each with why. */

const SRC = fileURLToPath(new URL('.', import.meta.url))
const files = (readdirSync(SRC, { recursive: true }) as string[]).map((f) => relative(SRC, join(SRC, f)).replaceAll('\\', '/'))
const read = (f: string) => readFileSync(join(SRC, f), 'utf8')

/** why each fixed box is allowed, and for which property: no text in it, a table cell (a width there is a floor, the
 *  cell grows), a decision-card slot (v0.8 fixed slots: `overflow: hidden` and a line clamp by design, measured in
 *  decision-card-v08), or the screen-high side rail (100vh, its menu fits at 600px high — measured in tile-overflow) */
type Kind = 'no-text' | 'table-cell' | 'clipped-slot' | 'viewport'
const ALLOW: Record<string, { kind: Kind; props: string[] }> = {
  '.rail': { kind: 'viewport', props: ['height'] },
  '.bar': { kind: 'no-text', props: ['height'] }, '.bar .axis': { kind: 'no-text', props: ['width'] },
  '.gauge .track': { kind: 'no-text', props: ['height'] }, '.gauge .line': { kind: 'no-text', props: ['width'] },
  '.xbar': { kind: 'no-text', props: ['height'] }, '.xsw': { kind: 'no-text', props: ['width', 'height'] },
  '.legend .sw': { kind: 'no-text', props: ['width'] }, '.prot-chip::before': { kind: 'no-text', props: ['height'] },
  '.plan-grid tbody th': { kind: 'table-cell', props: ['width'] }, '.holding-table th': { kind: 'table-cell', props: ['width'] },
  ...Object.fromEntries(['.dc-head', '.dc-price', '.dc-exec', '.dc-body', '.dc-body-hd', '.dc-sum', '.dc-ev']
    .map((s) => [s, { kind: 'clipped-slot' as Kind, props: ['height'] }])),
}

/** a size that follows the content or the container; anything else (a length, calc() of lengths, a var(), clamp())
 *  is a fixed box (review 1R #1: `calc(2.8rem)` and `!important` went through) */
const FREE = /^(auto|none|0|fit-content|min-content|max-content|inherit|initial|unset|revert)$/
export const fixedValue = (v: string) => { const x = v.replace(/!\s*important/, '').trim(); return !FREE.test(x) && !x.includes('%') }

/** each CSS rule (comments out) × selector × property that fixes a box: `width` · `height` · `inline-size` ·
 *  `block-size` · `flex-basis`, and a `flex` that neither grows nor shrinks (`0 0 2.8rem`); not min-/max-, not
 *  line-height */
export function fixedBoxes(css: string, path = 'app.css') {
  const text = css.replace(/\/\*[\s\S]*?\*\//g, (c) => c.replace(/[^\n]/g, ' '))
  return [...text.matchAll(/([^{}]+)\{([^{}]*)\}/g)].flatMap((m) => {
    const props = [...m[2].matchAll(/(?<![-\w])(width|height|inline-size|block-size|flex-basis|flex)\s*:\s*([^;]+)/g)]
      .map((d) => d[1] === 'flex' ? ['flex', /^0\s+0\s+(\S+)/.exec(d[2].trim())?.[1] ?? 'auto'] : [d[1], d[2]])
      .filter(([, v]) => fixedValue(v)).map(([prop]) => prop)
    const at = `${path}:${text.slice(0, m.index ?? 0).split('\n').length + (m[1].match(/^\s*/)?.[0].split('\n').length ?? 1) - 1}`
    return m[1].split(',').flatMap((s) => props.map((prop) => ({ sel: s.trim().replace(/\s+/g, ' '), prop, body: m[2], at })))
  })
}

/** a fixed `width` · `height` · `inlineSize` · `blockSize` · `flexBasis` in a JSX `style={{ … }}` (`'3rem'`, `28`,
 *  `'calc(…)'`); a chart's geometry constants (`{ width: 1000 }` in perfLogic) are no style */
export function inlineFixed(src: string) {
  const styles = [...src.matchAll(/style=\{\{([\s\S]*?)\}\}/g)].map((m) => m[1] + ',').join('')
  return [...styles.matchAll(/(?<![-\w])(width|height|inlineSize|blockSize|flexBasis)\s*:\s*((['"`])[^'"`]*\3|-?[\d.]+)(?=\s*[,}])/g)]
    .filter((m) => (m[3] ? fixedValue(m[2].slice(1, -1)) : Number(m[2]) !== 0)).map((m) => m[0])
}

const css = files.filter((f) => f.endsWith('.css')).flatMap((f) => fixedBoxes(read(f), f))

describe('fixed boxes (tile-overflow: text outgrew a fixed box twice)', () => {
  it('no CSS rule gives a fixed width or height to anything but the listed boxes', () => {
    expect(css.filter((b) => !ALLOW[b.sel]?.props.includes(b.prop)).map((b) => `${b.at} ${b.sel} ${b.prop}`)).toEqual([])
  })
  it('every listed box is still there, a table cell is a th/td, a card slot clips', () => {
    const stale = Object.entries(ALLOW).flatMap(([s, a]) => a.props.filter((p) => !css.some((b) => b.sel === s && b.prop === p)).map((p) => `${s} ${p}`))
    expect(stale).toEqual([])                                                                   // no stale entry
    for (const b of css.filter((x) => ALLOW[x.sel]?.kind === 'table-cell')) expect(b.sel).toMatch(/\b(th|td)$/)
    for (const b of css.filter((x) => ALLOW[x.sel]?.kind === 'clipped-slot')) expect(b.body, b.sel).toMatch(/overflow:\s*hidden/)
    for (const b of css.filter((x) => ALLOW[x.sel]?.kind === 'viewport')) expect(b.body, b.sel).toMatch(/height:\s*100vh/)
  })
  it('no component or page sets a fixed width or height in a style object', () => {
    const tsx = files.filter((f) => /\.tsx?$/.test(f) && !/\.test\.tsx?$/.test(f))
    expect(tsx.flatMap((f) => inlineFixed(read(f)).map((m) => `${f} ${m}`))).toEqual([])
  })
  it('catches the 결정 이력 tile as it was, and leaves a minimum width, a percentage and line-height alone', () => {
    expect(fixedBoxes('.tl { width: 2.8rem; height: 2.8rem; display: grid; }').map((b) => `${b.sel} ${b.prop}`)).toEqual(['.tl width', '.tl height'])
    expect(fixedBoxes('.tl { width: 2.8rem !important; height: calc(2.8rem); inline-size: var(--t); flex: 0 0 3rem; }').map((b) => b.prop))
      .toEqual(['width', 'height', 'inline-size', 'flex'])
    expect(fixedBoxes('.s { flex: 1 1 11rem; width: calc(100% - 2rem); height: 0; block-size: fit-content; }')).toEqual([])
    expect(fixedBoxes('.a, .b {\n  height: 24px;\n}').map((b) => b.sel)).toEqual(['.a', '.b'])
    expect(fixedBoxes('.tl { min-width: 4.4rem; max-width: 9rem; line-height: 1.5; width: 50%; height: auto; }')).toEqual([])
    expect(fixedBoxes('/* .tl { width: 2.8rem } */ .x { color: red; }')).toEqual([])
    expect(inlineFixed(`<i style={{ width: '2.8rem' }} /> <b style={{ height: 28 }} /> <u style={{ flexBasis: 'calc(3rem)' }} />`))
      .toEqual(["width: '2.8rem'", 'height: 28', "flexBasis: 'calc(3rem)'"])
    expect(inlineFixed(`<th style={{ width: '34%' }}> <i style={{ width: \`\${s * 100}%\` }} />`)).toEqual([])
    expect(inlineFixed("export const PC = { width: 1000, height: 210 }")).toEqual([])
  })
})
