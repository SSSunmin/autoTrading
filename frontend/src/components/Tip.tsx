import { Tooltip } from '@bv-ds/ui'
import { IconInfoCircle } from '@bv-ds/icons'

import { api } from '../api/client'
import type { Market, RulesSettings } from '../api/types'
import { GLOSSARY, rulesOf, type Term } from '../glossary'
import { useApi } from '../hooks/useApi'
import { useMarket } from '../hooks/useMarket'

const cached: Partial<Record<Market, Promise<RulesSettings>>> = {}

/** the market's rule values, loaded the first time a tip opens (not with the page) and kept; a failed load is retried */
function useRules(market: Market) {
  return useApi(() => (cached[market] ??= api.rules(market).catch((e: unknown) => { delete cached[market]; throw e })), [market])
}

function TipBody({ k }: { k: Term }) {
  const [market] = useMarket()
  const s = useRules(market).data
  const g = GLOSSARY[k]
  return <><span className="tip-title">{g.title}</span><span className="tip-text">{g.text(s ? rulesOf(s, market) : null)}</span></>
}

/** ⓘ beside a term (glossary-tips): the DS white tooltip (floating-ui flip · shift, so it stays on screen; Esc closes it)
 *  opens on hover and on focus. A click or Enter re-focuses the button so it opens again after Esc, and a tap on a
 *  phone opens it (Safari does not focus a tapped button by itself). ``term``: the text before the ⓘ where a line can
 *  wrap — its last word and the ⓘ are kept on one line (measured: the ⓘ fell to a line of its own; a word joiner does
 *  not hold before an inline-block in Chrome). */
export function Tip({ k, term }: { k: Term; term?: string }) {
  const tip = (
    <Tooltip variant="white" content={<TipBody k={k} />}>
      <button type="button" className="tip" aria-label={`${GLOSSARY[k].title} 설명`}
        onClick={(e) => { e.stopPropagation(); e.currentTarget.blur(); e.currentTarget.focus() }}>
        <IconInfoCircle size={16} aria-hidden />
      </button>
    </Tooltip>
  )
  if (term == null) return tip
  const at = term.lastIndexOf(' ') + 1
  return <>{term.slice(0, at)}<span className="tip-term">{term.slice(at)}{tip}</span></>
}

