import type { Api, Choice } from './api'
import { changeAgentBrowser, watchTab } from './AgentBrowser'
import type { InboxItem } from './needs'

interface AnswerActionsProps {
  api: Api
  item: InboxItem
  label: string
  waiting: boolean
  onAnswer: (item: InboxItem, option: Choice) => void
  onFail: (paneId: string, error: unknown) => void
}

export function AnswerActions({ api, item, label, waiting, onAnswer, onFail }: AnswerActionsProps) {
  const { pane } = item
  return <>
    {item.kind === 'browser' && <div className="inbox-actions" role="group" aria-label={`Answer ${label}`}>
      {item.tab && <button type="button" className="inbox-yes" onClick={() => watchTab(item.tab!, pane.id)}>Open the page</button>}
      <button type="button" onClick={() => void changeAgentBrowser(api, 'browser/ask/dismiss', { id: item.ask })
        .catch(error => onFail(pane.id, error))}>Done</button>
    </div>}
    {(item.yes || item.always || item.no || item.menu) && <div className="inbox-actions" role="group" aria-label={`Answer ${label}`}>
      {item.yes && <button type="button" className="inbox-yes" disabled={waiting} title={item.yes.text} onClick={() => void onAnswer(item, item.yes!)}>Yes <kbd>Y</kbd></button>}
      {item.always && <button type="button" disabled={waiting} title={item.always.text} onClick={() => void onAnswer(item, item.always!)}><span>{item.always.text}</span><kbd>A</kbd></button>}
      {item.no && <button type="button" disabled={waiting} title={item.no.text} onClick={() => void onAnswer(item, item.no!)}>No <kbd>N</kbd></button>}
      {item.menu?.map(option => <button type="button" key={option.id} disabled={waiting} title={option.text} onClick={() => void onAnswer(item, option)}><kbd>{option.id}</kbd><span>{option.text}</span></button>)}
    </div>}
  </>
}
