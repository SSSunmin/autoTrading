/// <reference types="node" />
import { readdirSync, readFileSync } from 'node:fs'
import { join, relative } from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

/** type-scale (2026-11-16): the screens had 13 font sizes, 10–11px text and the smallest buttons. These read every
 *  source file in src/ from disk (vitest hands `?raw` CSS over empty), so a new literal size or a small button turns
 *  this red instead of waiting for a screenshot. */

const SRC = fileURLToPath(new URL('.', import.meta.url))
const files = (readdirSync(SRC, { recursive: true }) as string[]).map((f) => './' + relative(SRC, join(SRC, f)).replaceAll('\\', '/'))
const read = (paths: string[]) => paths.map((path) => ({ path, text: readFileSync(join(SRC, path), 'utf8') }))
const css = read(files.filter((f) => f.endsWith('.css')))
/** .tsx and .ts (a style object can sit in either), tests left out */
const tsx = read(files.filter((f) => /\.tsx?$/.test(f) && !/\.test\.tsx?$/.test(f)))

/** the five sizes (app.css :root) — the only names a font-size may use */
const SCALE = ['--app-fs-title', '--app-fs-heading', '--app-fs-sub', '--app-fs-body', '--app-fs-small']
const line = (text: string, at: number) => text.slice(0, at).split('\n').length

/** `font-size` values in CSS, `fontSize` in JSX styles and `font-size=` on SVG text, each with where it is; the
 *  `font:` shorthand too (it sets a size), where only `inherit` is let through below */
export function fontSizes(files: { path: string; text: string }[]) {
  return files.flatMap(({ path, text }) => [
    ...text.matchAll(/font-size\s*:\s*([^;}]+)/g),
    ...text.matchAll(/fontSize\s*:\s*([^,}\n]+)/g),
    ...text.matchAll(/font-?[sS]ize=\{?["'`]?([^"'`}\s]+)/g),
    ...text.matchAll(/(?<![-\w])font\s*:\s*([^;}\n]+)/g),
  ].map((m) => ({ at: `${path}:${line(text, m.index ?? 0)}`, value: m[1].trim().replace(/^['"`]|['"`],?$/g, '') })))
}

/** a size outside the scale: a literal (px, rem, em, %, a keyword) or any token but the five; `inherit` (a `font:`
 *  shorthand that only resets a button's font) is no size of its own */
export function offScale(value: string): boolean {
  if (value === 'inherit') return false
  const names = [...value.matchAll(/var\(\s*(--[\w-]+)/g)].map((m) => m[1])
  const rest = value.replace(/var\([^)]*\)/g, '')
  return names.length === 0 || names.some((n) => !SCALE.includes(n)) || /\d|\b(small|smaller|larger|medium)\b/.test(rest)
}

/** A JSX opening tag from `start` to its `>`, skipping `>` inside `{…}` (`onClick={() => …}`) and inside strings. */
function openingTag(text: string, start: number): string {
  let depth = 0, quote = ''
  for (let i = start; i < text.length; i++) {
    const c = text[i]
    if (quote) { if (c === quote) quote = ''; continue }
    if (c === '"' || c === "'" || c === '`') quote = c
    else if (c === '{') depth++
    else if (c === '}') depth--
    else if (c === '>' && depth === 0) return text.slice(start, i + 1)
  }
  return text.slice(start)
}

/** the smallest size of a DS button: Agent `size="sm"`, Button `size="small"`, IconButton `size={20}` — written as a
 *  string or in braces, before or after any other prop. The controls beside the buttons too (partial-sell 부속,
 *  2026-11-16): a segmented control reads as buttons, and a 28px field next to a 38px button is out of line */
const CONTROLS = 'AgentButton|Button|IconButton|AgentIconButton|AgentSegmentedControl|SegmentedControl|AgentTextField|TextField'
  + '|AgentTextarea|Textarea|AgentSelect|Select'
export function smallButtons(files: { path: string; text: string }[]) {
  return files.flatMap(({ path, text }) => [...text.matchAll(new RegExp(`<(${CONTROLS})\\b`, 'g'))]
    .map((m) => ({ m, tag: openingTag(text, m.index ?? 0) }))
    .filter(({ tag }) => /\ssize\s*=\s*(\{\s*)?(["'`](sm|small|20)["'`]|20(?!\d))\s*\}?/.test(tag))
    .map(({ m, tag }) => `${path}:${line(text, m.index ?? 0)} <${m[1]} …${tag.match(/size\s*=\s*\S+?(?=[\s/>]|$)/)?.[0]}>`))
}

describe('type scale — five sizes, none under 12px (the screens had 13 sizes and 10–11px text)', () => {
  it('finds the sources it guards', () => {
    expect(css.map((f) => f.path)).toContain('./app.css')
    expect(tsx.length).toBeGreaterThan(20)
  })

  it('sets every font-size through the five --app-fs-* names, never a literal or another token', () => {
    const found = fontSizes([...css, ...tsx])
    expect(found.length).toBeGreaterThan(40)
    expect(found.filter((f) => offScale(f.value))).toEqual([])
  })

  it('defines the five names once, each a DS --bv-font-size-* token (12px is the smallest: caption-1)', () => {
    const app = css.find((f) => f.path === './app.css')!.text
    const defs = SCALE.map((n) => [n, app.match(new RegExp(`${n}\\s*:\\s*([^;]+);`))?.[1].trim()])
    expect(defs).toEqual([
      ['--app-fs-title', 'var(--bv-font-size-title-3)'],
      ['--app-fs-heading', 'var(--bv-font-size-heading-2)'],
      ['--app-fs-sub', 'var(--bv-font-size-headline-2)'],
      ['--app-fs-body', 'var(--bv-font-size-body-3-normal)'],
      ['--app-fs-small', 'var(--bv-font-size-caption-1)'],
    ])
  })

  it('catches what it is for: a literal, a stray token, a relative size, a size in the font shorthand', () => {
    const probe = [{ path: 'x.css', text: '.a { font-size: 11px } .b { font-size: var(--bv-font-size-caption-2) } .c { font-size: 0.95em } .d { font-size: var(--app-fs-small) }'
      + ' .e { font: 11px/1.4 sans-serif } .f { font: inherit; }' },
      { path: 'x.tsx', text: '<text fontSize="10">x</text> <b style={{ fontSize: 13 }}>y</b>' },
      { path: 'x.ts', text: "export const S = { fontSize: '11px' }" }]
    expect(fontSizes(probe).map((f) => [f.value, offScale(f.value)])).toEqual([
      ['11px', true], ['var(--bv-font-size-caption-2)', true], ['0.95em', true], ['var(--app-fs-small)', false],
      ['11px/1.4 sans-serif', true], ['inherit', false],
      ['13', true], ['10', true], ['11px', true],
    ])
  })
})

describe('buttons and the controls beside them — never the smallest DS size (the user: "버튼은 가장 작은 걸로는 안 했으면")', () => {
  it('uses no small button, segmented control, field or select anywhere in src/', () => {
    for (const c of ['<AgentButton', '<AgentSegmentedControl', '<AgentTextField', '<AgentTextarea', '<AgentSelect']) {
      expect(tsx.some((f) => f.text.includes(c))).toBe(true)
    }
    expect(smallButtons(tsx)).toEqual([])
  })

  it('catches each smallest size: on one line or across lines, after an arrow prop, in braces', () => {
    const probe = [{ path: 'x.tsx', text: '<AgentButton size="sm">a</AgentButton> <AgentButton\n  variant="primary"\n  size="sm"\n>b</AgentButton>'
      + ' <Button size="small" /> <IconButton size={20} icon={i} /> <AgentButton size="md">ok</AgentButton> <IconButton size={24} icon={i} />\n'
      + '<AgentButton variant="primary" onClick={() => setOpen(a > b)} size="sm">c</AgentButton> <AgentButton size={\'sm\'}>d</AgentButton>'
      + ' <AgentButton title="a > b" size={"sm"}>e</AgentButton> <IconButton size={200} icon={i} />' }]
    expect(smallButtons(probe)).toEqual([
      'x.tsx:1 <AgentButton …size="sm">', 'x.tsx:1 <AgentButton …size="sm">', 'x.tsx:4 <Button …size="small">', 'x.tsx:4 <IconButton …size={20}>',
      'x.tsx:5 <AgentButton …size="sm">', "x.tsx:5 <AgentButton …size={'sm'}>", 'x.tsx:5 <AgentButton …size={"sm"}>',
    ])
  })

  it('catches the small segmented control, field, textarea and select, and leaves their default alone', () => {
    const probe = [{ path: 'x.tsx', text: '<AgentSegmentedControl options={o} value={v} onChange={(x) => set(x)} size="sm" />\n'
      + '<AgentTextField label="금액" size="sm" /> <AgentTextarea size={\'sm\'} /> <AgentSelect\n  size="sm"\n  options={o} />\n'
      + '<AgentTextField label="사유" /> <AgentSelect options={o} size="md" /> <SelectOption size="sm" />' }]
    expect(smallButtons(probe)).toEqual([
      'x.tsx:1 <AgentSegmentedControl …size="sm">', 'x.tsx:2 <AgentTextField …size="sm">', "x.tsx:2 <AgentTextarea …size={'sm'}>",
      'x.tsx:2 <AgentSelect …size="sm">',
    ])
  })
})

/** What the screens say, outside comments and imports: no script path, API route or internal stage number. */
export function devText(files: { path: string; text: string }[]) {
  return files.flatMap(({ path, text }) => {
    const code = text.replace(/\/\*[\s\S]*?\*\//g, (c) => c.replace(/[^\n]/g, ' '))              // keep line numbers
      .replace(/(^|[^:])\/\/[^\n]*/g, '$1').replace(/^import [^\n]*/gm, '')
    return [...code.matchAll(/scripts\/|\b\w+\.py\b|\/api\/|\b(POST|GET|PUT|DELETE) \/|단계 ?\d/g)]
      .map((m) => `${path}:${line(code, m.index ?? 0)} ${m[0]}`)
  })
}

describe('screen text — no developer wording (type-scale: script paths turned up in four places, review 1R · 2R)', () => {
  it('shows no script path, API route or stage number in any component or page', () => {
    expect(devText(tsx.filter((f) => f.path.endsWith('.tsx')))).toEqual([])
  })

  it('catches it in a string, a template and JSX text, and leaves comments and imports alone', () => {
    const probe = [{ path: 'x.tsx', text: "import { api } from '../api/client'\n// runs scripts/daily_loop.py\n/** POST /api/trading/halt */\n"
      + "const a = '모의 실행(scripts/paper_execute.py)'\nconst b = `(단계 5에서 연결)`\n<p>누르면 POST /api/trading/mode</p>" }]
    expect(devText(probe)).toEqual(['x.tsx:4 scripts/', 'x.tsx:4 paper_execute.py', 'x.tsx:5 단계 5', 'x.tsx:6 POST /'])
  })
})
