import { ArrowSquareOut, BookOpen, CaretDown, Info, Question } from '@phosphor-icons/react'
import type { Grounding } from '../types/api'

export function safeLink(value: string | null | undefined) {
  if (!value) return undefined
  try {
    const url = new URL(value)
    return ['https:', 'http:'].includes(url.protocol) && !url.username && !url.password ? url.href : undefined
  } catch { return undefined }
}

export function SourceDetails({ grounding, fieldLabels = {}, pausedFields = new Set<string>(), resultStatus }: {
  grounding: Grounding; fieldLabels?: Record<string, string>; pausedFields?: Set<string>; resultStatus?: string
}) {
  const outside = resultStatus === 'out_of_scope' || resultStatus === 'unsupported_action'
  const hasSources = grounding.sources.length > 0
  const answered = grounding.status === 'ANSWER' && hasSources && !outside
  const clarify = grounding.status === 'NEED_CLARIFICATION' && !outside
  const label = outside ? (resultStatus === 'unsupported_action' ? 'Chưa hỗ trợ hành động' : 'Ngoài phạm vi')
    : answered ? 'Nguồn tham chiếu' : clarify ? 'Cần làm rõ'
      : grounding.status === 'ANSWER' ? 'Không có nguồn đính kèm' : 'Chưa đủ dữ liệu'
  const Icon = answered ? BookOpen : clarify ? Question : Info
  const sources = grounding.sources.filter((source, index, all) =>
    all.findIndex(item => item.source_id === source.source_id && item.url === source.url) === index)
  const checklist = grounding.checklist?.filter(item => !pausedFields.has(item.field))
  const missing = grounding.missing_information.filter(field => !pausedFields.has(field))

  return <div className="source-reference">
    {grounding.data_classification === 'D0_SYSTEM_SMOKE' && <p className="source-warning"><Info size={18} />Dữ liệu mô phỏng, không phải hướng dẫn thực tế.</p>}
    <details className={`grounding grounding-${answered ? 'answer' : 'notice'}`}>
      <summary><Icon size={18} /><span>{label}</span>
        {hasSources && <span className="source-summary-title">{sources[0].title}{sources.length > 1 ? ` và ${sources.length - 1} nguồn khác` : ''}</span>}
        <CaretDown size={16} className="disclosure-caret" />
      </summary>
      <div className="source-content">
        {sources.map(source => <p className="source-note" key={`${source.source_id}:${source.url}`}>
          Nguồn: {safeLink(source.url)
            ? <a href={safeLink(source.url)} target="_blank" rel="noreferrer noopener">{source.title}<ArrowSquareOut size={16} /></a>
            : <strong>{source.title}</strong>}
        </p>)}
        {!hasSources && <p className="source-note">{outside ? 'Yêu cầu này không được bộ dữ liệu hiện tại hỗ trợ.'
          : grounding.status === 'ANSWER' ? 'Nội dung này chưa có đoạn nguồn đính kèm.' : 'Chưa có đoạn nguồn đủ để xác nhận nội dung được hỏi.'}</p>}
        {missing.length > 0 && <p className="source-note">Cần bổ sung: {missing.map(field => fieldLabels[field] ?? field).join(', ')}.</p>}
        <details className="technical-details"><summary>Chi tiết đối chiếu<CaretDown size={16} /></summary>
          {checklist?.length ? <dl className="answer-checklist">{checklist.map(item => <div key={item.field}><dt>{item.label}</dt><dd>{item.value}</dd></div>)}</dl> : null}
          <p>Phiên bản nguồn: <code>{grounding.knowledge_version ?? grounding.corpus_version}</code></p>
          {grounding.verification && <p>{grounding.verification.fallback_used ? 'Hệ thống dùng nội dung trích xuất từ nguồn thay cho bản diễn đạt của model.' : 'Nội dung đã qua bước đối chiếu với nguồn.'}</p>}
          {grounding.sources.map(source => <code className="fragment-id" key={source.fragment_id}>{source.fragment_id}</code>)}
        </details>
      </div>
    </details>
  </div>
}
