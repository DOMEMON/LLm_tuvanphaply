import type { ReactNode } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { ArrowSquareOut } from '@phosphor-icons/react'
import { safeLink } from './SourceDetails'
import type { Grounding, Message } from '../types/api'

function Markdown({ content }: { content: string }) {
  return <div className="answer-markdown"><ReactMarkdown remarkPlugins={[remarkGfm]}
    urlTransform={url => safeLink(url) ?? ''}
    components={{ img: ({ alt }) => <span>{alt}</span>, a: ({ href, children }) =>
      <a href={safeLink(href)} target="_blank" rel="noreferrer noopener">{children}<ArrowSquareOut size={15} /></a> }}>
    {content}
  </ReactMarkdown></div>
}

const statuses: Record<string, string> = {
  answered: 'Đã trả lời', clarification_needed: 'Cần làm rõ', insufficient_data: 'Thiếu dữ liệu',
  out_of_scope: 'Ngoài phạm vi', unsupported_action: 'Chưa hỗ trợ hành động',
}

export function AnswerContent({ message, renderGrounding, onChecklist }: {
  message: Message; renderGrounding: (grounding: Grounding, resultStatus?: string) => ReactNode
  onChecklist?: (messageId: string, procedureId?: string) => void
}) {
  const parts = message.grounding?.parts
  const blocks = parts?.map(part => `### ${part.title}\n\n${part.answer}`)
  const joined = blocks?.join('\n\n') ?? ''
  // Only re-layout an exact, complete copy of the stored response. Historical
  // or mismatched payloads keep the entire original content, including warnings.
  const canSeparate = Boolean(joined && parts?.every(part => part.title && part.answer)
    && message.content.endsWith(joined))
  const prefix = canSeparate ? message.content.slice(0, -joined.length).trimEnd() : ''
  return <>
    {canSeparate ? prefix && <Markdown content={prefix} /> : <Markdown content={message.content} />}
    {parts?.length ? parts.map((part, index) => <section className="answer-part" key={`${part.task_id}:${index}`} aria-label={part.title}>
      {canSeparate ? <Markdown content={blocks![index]} /> : <p className="source-note">Đối chiếu: <strong>{part.title}</strong></p>}
      {part.result_status && part.result_status !== 'answered' && <p className="answer-status">{statuses[part.result_status] ?? 'Cần đối chiếu thêm'}</p>}
      {renderGrounding(part.grounding, part.result_status ?? (part.kind === 'outside' ? 'out_of_scope' : undefined))}
      {onChecklist && part.procedure_id && part.grounding.checklist?.some(item => item.field === 'required_documents') &&
        <button className="text-button checklist-create" onClick={() => onChecklist(message.id, part.procedure_id!)}>Lưu checklist: {part.title}</button>}
    </section>) : message.grounding && renderGrounding(message.grounding)}
  </>
}
