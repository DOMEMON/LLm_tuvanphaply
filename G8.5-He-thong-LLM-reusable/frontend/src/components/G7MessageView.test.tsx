import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, it, vi } from 'vitest'
import { MessageView } from './MessageView'
import type { Grounding, Message } from '../types/api'

function grounding(pid: string): Grounding {
  return { request_id: 'r', evidence_bundle_id: 'b', status: 'ANSWER', corpus_version: 'v',
    data_classification: 'D2_COMPANY_REAL', missing_information: [],
    sources: [{ fragment_id: pid + ':docs', source_id: pid, title: pid, url: null, metadata: {} }],
    checklist: [{ field: 'required_documents', label: 'Hồ sơ', value: 'Giấy tờ ' + pid, evidence_ids: [pid + ':docs'] }] }
}
function message(parts: Grounding['parts']): Message {
  return { id: 'message', conversation_id: 'c', role: 'assistant', content: 'Hai phần trả lời.',
    status: 'completed', created_at: '', grounding: { ...grounding('root'), parts } }
}
it('offers separate procedure-bound checklist actions for a multi-procedure answer', async () => {
  const onChecklist = vi.fn()
  const parts = ['khai sinh', 'kết hôn'].map((pid, i) => ({ task_id: String(i), kind: 'procedure',
    procedure_id: pid, title: pid, answer: '', grounding: grounding(pid) }))
  render(<MessageView message={message(parts)} onChecklist={onChecklist} onFeedback={vi.fn()} onError={vi.fn()} />)
  const buttons = screen.getAllByRole('button', { name: /Lưu checklist:/ })
  expect(buttons).toHaveLength(2)
  await userEvent.click(buttons[0])
  expect(onChecklist).toHaveBeenLastCalledWith('message', 'khai sinh')
  await userEvent.click(buttons[1])
  expect(onChecklist).toHaveBeenLastCalledWith('message', 'kết hôn')
  expect(screen.queryByRole('button', { name: 'Lưu checklist hồ sơ' })).not.toBeInTheDocument()
})
it('does not create a checklist for an outside-scope part', () => {
  const part = { task_id: 'a', kind: 'outside', procedure_id: null, title: 'Ngoài phạm vi', answer: '',
    grounding: { ...grounding('x'), status: 'INSUFFICIENT_DATA' as const, checklist: [], sources: [] } }
  render(<MessageView message={message([part])} onChecklist={vi.fn()} onFeedback={vi.fn()} onError={vi.fn()} />)
  expect(screen.queryByRole('button', { name: /Lưu checklist/ })).not.toBeInTheDocument()
})
it('keeps historical single-procedure messages readable', () => {
  render(<MessageView message={message(undefined)} onChecklist={vi.fn()} onFeedback={vi.fn()} onError={vi.fn()} />)
  expect(screen.getByRole('button', { name: 'Lưu checklist hồ sơ' })).toBeInTheDocument()
})

it('does not mislabel a backend processing failure as an unsent message', () => {
  const failed = { ...message(undefined), role: 'user' as const, status: 'failed' as const,
    content: 'Một yêu cầu đã tới máy chủ' }
  render(<MessageView message={failed} onFeedback={vi.fn()} onError={vi.fn()} />)
  expect(screen.getByText(/Chưa xử lý được yêu cầu/)).toBeInTheDocument()
  expect(screen.queryByText('Chưa gửi thành công')).not.toBeInTheDocument()
})
