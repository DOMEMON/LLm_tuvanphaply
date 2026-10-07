import { useState } from 'react'
import { Check, CheckCircle, Copy, CaretDown, ChatCircleDots, ArrowSquareOut, SealCheck } from '@phosphor-icons/react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { deleteMessageFeedback, putMessageFeedback } from '../services/api'
import type { FeedbackRelevance, FeedbackSatisfaction, Grounding, Message, MessageFeedback } from '../types/api'

const PAUSED_FIELDS = new Set<string>()
function safeLink(value: string | null | undefined) {
  if (!value) return undefined
  try { const url = new URL(value); return ['https:', 'http:'].includes(url.protocol) ? url.href : undefined }
  catch { return undefined }
}
export function GroundingPanel({ grounding, fieldLabels = {} }: { grounding: Grounding; fieldLabels?: Record<string, string> }) {
  const checklist = grounding.checklist?.filter((item) => !PAUSED_FIELDS.has(item.field))
  const missing = grounding.missing_information.filter((field) => !PAUSED_FIELDS.has(field))
  const label = grounding.status === 'ANSWER' ? 'Nguồn tham chiếu' : grounding.status === 'NEED_CLARIFICATION' ? 'Cần làm rõ' : 'Chưa đủ dữ liệu'
  const titles = [...new Set(grounding.sources.map((source) => source.title))]
  if (!titles.length && grounding.procedure_title) titles.push(grounding.procedure_title)
  return <details className={`grounding grounding-${grounding.status.toLowerCase()}`}>
    <summary><SealCheck size={17} /><span>{label}</span>{grounding.sources.length > 0 && <span className="source-count">{grounding.sources.length}</span>}<CaretDown size={14} /></summary>
    <div className="source-content">
      {titles.map((title) => <p className="source-note" key={title}>Nguồn: <strong>{title}</strong></p>)}
      <details className="technical-details"><summary>Chi tiết đối chiếu</summary>
        {grounding.data_classification === 'D0_SYSTEM_SMOKE' && <p>Dữ liệu mô phỏng, không phải hướng dẫn thực tế.</p>}
        {checklist?.length ? <dl className="answer-checklist">{checklist.map((item) => <div key={item.field}><dt>{item.label}</dt><dd>{item.value}</dd></div>)}</dl> : null}
        {missing.length > 0 && <p>Cần bổ sung: {missing.map((field) => fieldLabels[field] ?? field).join(', ')}.</p>}
        {grounding.sources.filter((source, index, sources) => safeLink(source.url) && sources.findIndex((item) => item.url === source.url) === index).map((source) => <p key={source.source_id}><a href={safeLink(source.url)} target="_blank" rel="noreferrer noopener">{source.title}<ArrowSquareOut size={14} /></a></p>)}
        <p>Phiên bản nguồn: <code>{grounding.knowledge_version ?? grounding.corpus_version}</code></p>
        {grounding.verification && <p>{grounding.verification.fallback_used ? 'Hệ thống dùng nội dung trích xuất từ nguồn thay cho bản diễn đạt của model.' : 'Nội dung đã qua bước đối chiếu với nguồn.'}</p>}
        {grounding.sources.map((source) => <code className="fragment-id" key={source.fragment_id}>{source.fragment_id}</code>)}
      </details>
    </div>
  </details>
}
export function FeedbackPanel({ messageId, initial, onChange, onError }: {
  messageId: string; initial?: MessageFeedback; onChange: (feedback: MessageFeedback | null) => void; onError: (error: unknown) => void
}) {
  const [relevance, setRelevance] = useState<FeedbackRelevance | ''>(initial?.relevance ?? '')
  const [satisfaction, setSatisfaction] = useState<FeedbackSatisfaction | ''>(initial?.satisfaction ?? '')
  const [reason, setReason] = useState(initial?.reason ?? '')
  const [busy, setBusy] = useState(false)
  const [saved, setSaved] = useState(Boolean(initial))
  const [validation, setValidation] = useState('')
  async function save() {
    if (busy) return
    if (!relevance || !satisfaction) {
      setValidation(!relevance && !satisfaction
        ? 'Bạn chọn mức độ phù hợp và mức độ hài lòng trước khi lưu nhé.'
        : !satisfaction ? 'Bạn chưa chọn mức độ hài lòng.' : 'Bạn chưa chọn mức độ phù hợp.')
      return
    }
    setValidation('')
    setBusy(true)
    try { onChange(await putMessageFeedback(messageId, { relevance, satisfaction, reason: reason.trim() || null })); setSaved(true) }
    catch (err) { onError(err) } finally { setBusy(false) }
  }
  async function remove() {
    setBusy(true)
    try { await deleteMessageFeedback(messageId); onChange(null); setSaved(false); setReason(''); setRelevance(''); setSatisfaction('') }
    catch (err) { onError(err) } finally { setBusy(false) }
  }
  return <section className="feedback" aria-label="Đánh giá câu trả lời">
    <div className="feedback-heading"><strong>Phản hồi câu trả lời</strong>{saved && <span><CheckCircle size={14} /> Đã lưu</span>}</div>
    <div className="feedback-row" role="group" aria-label="Câu trả lời có phù hợp không?"><span>Phù hợp?</span>{([['relevant', 'Phù hợp'], ['not_relevant', 'Không phù hợp']] as const).map(([value, label]) => <button key={value} aria-pressed={relevance === value} onClick={() => { setRelevance(value); setSaved(false) }} disabled={busy}>{label}</button>)}</div>
    <div className="feedback-row" role="group" aria-label="Bạn có hài lòng không?"><span>Hài lòng?</span>{([['satisfied', 'Hài lòng'], ['not_satisfied', 'Chưa hài lòng']] as const).map(([value, label]) => <button key={value} aria-pressed={satisfaction === value} onClick={() => { setSatisfaction(value); setSaved(false) }} disabled={busy}>{label}</button>)}</div>
    <textarea aria-label="Lý do phản hồi" placeholder="Bạn muốn chúng tôi cải thiện điều gì? (không bắt buộc)" value={reason} onChange={(event) => { setReason(event.target.value); setSaved(false) }} maxLength={1000} rows={2} disabled={busy} />
    {(!relevance || !satisfaction) && <p className="form-note">Chọn một mục ở mỗi dòng “Phù hợp?” và “Hài lòng?”. Phần lý do không bắt buộc.</p>}
    {validation && (!relevance || !satisfaction) && <p className="inline-error" role="alert">{validation}</p>}
    <div className="feedback-actions">{initial && <button className="text-button" disabled={busy} onClick={() => void remove()}>Xóa phản hồi</button>}<button className="primary-button" disabled={busy} onClick={() => void save()}>{busy ? 'Đang lưu…' : initial ? 'Cập nhật' : 'Lưu phản hồi'}</button></div>
  </section>
}
export function MessageView({ message, feedback, onFeedback, onError, onChecklist, fieldLabels = {} }: {
  fieldLabels?: Record<string, string>;
  message: Message; feedback?: MessageFeedback; onFeedback: (value: MessageFeedback | null) => void; onError: (err: unknown) => void; onChecklist?: (messageId: string, procedureId?: string) => void
}) {
  const [copied, setCopied] = useState(false)
  if (message.role === 'user') return <article className={`message user ${message.status}`} aria-label="Câu hỏi của bạn"><div className="user-bubble">{message.content}</div>{message.status === 'failed' && <small>Chưa xử lý được yêu cầu. Bạn xem thông báo lỗi và thử lại nhé.</small>}</article>
  async function copy() {
    try { await navigator.clipboard.writeText(message.content); setCopied(true); window.setTimeout(() => setCopied(false), 1800) }
    catch { onError(new Error('Không sao chép được. Bạn có thể chọn và sao chép nội dung trực tiếp.')) }
  }
  return <article className="message assistant" aria-label="Câu trả lời của trợ lý">
    <div className="assistant-identity"><span className="assistant-mark"><SealCheck size={18} weight="fill" /></span><span>Trợ lý dữ liệu</span></div>
    <div className="answer-markdown"><ReactMarkdown remarkPlugins={[remarkGfm]} urlTransform={(url) => safeLink(url) ?? ''} components={{ img: ({ alt }) => <span>{alt}</span>, a: ({ href, children }) => <a href={safeLink(href)} target="_blank" rel="noreferrer noopener">{children}<ArrowSquareOut size={13} /></a> }}>{message.content}</ReactMarkdown></div>
    {message.grounding?.parts?.length ? message.grounding.parts.map((part) => <section key={part.task_id} aria-label={part.title}>
      <p className="source-note">Đối chiếu: <strong>{part.title}</strong>{part.result_status && <span> · {({answered: "Đã trả lời", clarification_needed: "Cần làm rõ", insufficient_data: "Thiếu dữ liệu", out_of_scope: "Ngoài phạm vi", unsupported_action: "Chưa hỗ trợ hành động"} as Record<string, string>)[part.result_status] ?? part.result_status}</span>}</p>
      <GroundingPanel fieldLabels={fieldLabels} grounding={part.grounding} />
      {onChecklist && part.procedure_id && part.grounding.checklist?.some((item) => item.field === "required_documents") && <button className="text-button checklist-create" onClick={() => onChecklist(message.id, part.procedure_id!)}>Lưu checklist: {part.title}</button>}
    </section>) : message.grounding && <GroundingPanel fieldLabels={fieldLabels} grounding={message.grounding} />}
    {onChecklist && !message.grounding?.parts?.length && message.grounding?.status === 'ANSWER' && message.grounding.checklist?.some((item) => item.field === 'required_documents') && <button className="text-button checklist-create" onClick={() => onChecklist(message.id)}>Lưu checklist hồ sơ</button>}
    <div className="answer-actions"><button className="icon-button" aria-label={copied ? 'Đã sao chép' : 'Sao chép câu trả lời'} title={copied ? 'Đã sao chép' : 'Sao chép'} onClick={() => void copy()}>{copied ? <Check size={17} /> : <Copy size={17} />}</button><details className="feedback-disclosure"><summary><ChatCircleDots size={17} />{feedback ? 'Đã phản hồi' : 'Góp ý câu trả lời'}</summary><FeedbackPanel messageId={message.id} initial={feedback} onChange={onFeedback} onError={onError} /></details></div>
  </article>
}
