import { AnswerContent } from './AnswerContent'
import { SourceDetails } from './SourceDetails'
import { useState } from 'react'
import { Check, CheckCircle, Copy, ChatCircleDots } from '@phosphor-icons/react'
import { deleteMessageFeedback, putMessageFeedback } from '../services/api'
import type { FeedbackRelevance, FeedbackSatisfaction, Grounding, Message, MessageFeedback } from '../types/api'

const FIELDS: Record<string, string> = {
  procedure_id: 'thủ tục bạn muốn được hỗ trợ', field_intents: 'nội dung cần hỏi', jurisdiction: 'địa phương',
  as_of: 'Thời điểm', required_documents: 'Thành phần hồ sơ', fees: 'Lệ phí',
  processing_times: 'Thời gian giải quyết', receiving_authority: 'Nơi tiếp nhận',
  submission_methods: 'Hình thức nộp',
}
const PAUSED_FIELDS = new Set(['steps', 'legal_bases', 'applicant_scope'])
export function GroundingPanel({ grounding, resultStatus }: { grounding: Grounding; resultStatus?: string }) {
  return <SourceDetails grounding={grounding} fieldLabels={FIELDS} pausedFields={PAUSED_FIELDS} resultStatus={resultStatus} />
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
export function MessageView({ message, feedback, onFeedback, onError, onChecklist }: {
  message: Message; feedback?: MessageFeedback; onFeedback: (value: MessageFeedback | null) => void; onError: (err: unknown) => void; onChecklist?: (messageId: string, procedureId?: string) => void
}) {
  const [copied, setCopied] = useState(false)
  if (message.role === 'user') return <article className={`message user ${message.status}`} aria-label="Câu hỏi của bạn"><div className="user-bubble">{message.content}</div>{message.status === 'failed' && <small>Chưa xử lý được yêu cầu. Bạn xem thông báo lỗi và thử lại nhé.</small>}</article>
  async function copy() {
    try { await navigator.clipboard.writeText(message.content); setCopied(true); window.setTimeout(() => setCopied(false), 1800) }
    catch { onError(new Error('Không sao chép được. Bạn có thể chọn và sao chép nội dung trực tiếp.')) }
  }
  return <article className="message assistant" aria-label="Câu trả lời của trợ lý">
    <div className="assistant-identity"><span className="assistant-mark"><ChatCircleDots size={20} /></span><span>Trợ lý hành chính</span></div>
    <AnswerContent message={message} onChecklist={onChecklist} renderGrounding={(grounding, resultStatus) => <GroundingPanel grounding={grounding} resultStatus={resultStatus} />} />
    {onChecklist && !message.grounding?.parts?.length && message.grounding?.status === 'ANSWER' && message.grounding.checklist?.some((item) => item.field === 'required_documents') && <button className="text-button checklist-create" onClick={() => onChecklist(message.id)}>Lưu checklist hồ sơ</button>}
    <div className="answer-actions"><button className="icon-button" aria-label={copied ? 'Đã sao chép' : 'Sao chép câu trả lời'} title={copied ? 'Đã sao chép' : 'Sao chép'} onClick={() => void copy()}>{copied ? <Check size={17} /> : <Copy size={17} />}</button><details className="feedback-disclosure"><summary><ChatCircleDots size={17} />{feedback ? 'Đã phản hồi' : 'Góp ý câu trả lời'}</summary><FeedbackPanel messageId={message.id} initial={feedback} onChange={onFeedback} onError={onError} /></details></div>
  </article>
}
