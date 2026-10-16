import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import { AnswerContent } from './AnswerContent'
import { SourceDetails } from './SourceDetails'
import type { Grounding, Message } from '../types/api'

function grounding(status: Grounding['status'] = 'ANSWER', title = 'Tài liệu A'): Grounding {
  return { status, request_id: 'r', evidence_bundle_id: 'b', corpus_version: 'private-version',
    data_classification: 'D1_PUBLIC_DEMO', missing_information: [],
    sources: status === 'ANSWER' ? [{source_id: title, fragment_id: 'private-fragment', title,
      url: 'https://example.com/document', metadata: {}}] : [] }
}
function message(): Message {
  const parts = [{task_id:'a',kind:'procedure',procedure_id:'a',title:'Đối tượng A',answer:'Nội dung A.',grounding:grounding()},
    {task_id:'b',kind:'procedure',procedure_id:'b',title:'Đối tượng B',answer:'Chưa có thông tin B.',result_status:'insufficient_data',grounding:grounding('INSUFFICIENT_DATA')}]
  return {id:'msg',conversation_id:'c',role:'assistant',status:'completed',created_at:'',
    content:'Phạm vi nguồn: địa phương X.\n\n' + parts.map(part => `### ${part.title}\n\n${part.answer}`).join('\n\n'),
    grounding:{...grounding(),parts} }
}
const panel = (g: Grounding, status?: string) => <SourceDetails grounding={g} resultStatus={status} />

describe('Shared source presentation (domain-neutral fixtures)', () => {
  it('keeps each source beside its answer without losing the scope prefix or duplicating text', () => {
    render(<AnswerContent message={message()} renderGrounding={panel} />)
    expect(screen.getByText('Phạm vi nguồn: địa phương X.')).toBeInTheDocument()
    expect(within(screen.getByRole('region',{name:'Đối tượng A'})).getByText('Nội dung A.')).toBeInTheDocument()
    expect(within(screen.getByRole('region',{name:'Đối tượng B'})).getByText('Chưa đủ dữ liệu')).toBeInTheDocument()
    expect(screen.getAllByText('Nội dung A.')).toHaveLength(1)
  })
  it('falls back to the full stored message if parts are incomplete or mismatched', () => {
    const msg = message()
    msg.content = 'Nội dung gốc cùng cảnh báo phải giữ nguyên.'
    render(<AnswerContent message={msg} renderGrounding={panel} />)
    expect(screen.getByText(msg.content)).toBeInTheDocument()
    expect(screen.queryByText('Nội dung A.')).not.toBeInTheDocument()
  })
  it('shows meaningful source titles, lets users open the source, and hides technical IDs until requested', async () => {
    const user = userEvent.setup()
    render(<SourceDetails grounding={grounding()} />)
    expect(screen.getAllByText('Tài liệu A')[0]).toBeVisible()
    expect(screen.getByText('private-fragment')).not.toBeVisible()
    await user.click(screen.getByText('Nguồn tham chiếu'))
    expect(screen.getByRole('link',{name:'Tài liệu A'})).toHaveAttribute('href','https://example.com/document')
    expect(screen.getByText('private-fragment')).not.toBeVisible()
    await user.click(screen.getByText('Chi tiết đối chiếu'))
    expect(screen.getByText('private-fragment')).toBeVisible()
  })
  it('never makes an unsafe source URL clickable', async () => {
    const g = grounding()
    g.sources[0].url = 'javascript:alert(1)'
    render(<SourceDetails grounding={g} />)
    await userEvent.click(screen.getByText('Nguồn tham chiếu'))
    expect(screen.queryByRole('link')).not.toBeInTheDocument()
  })
  it('does not call a no-evidence ANSWER sourced or hide a simulation warning', () => {
    const g = {...grounding(),sources:[],data_classification:'D0_SYSTEM_SMOKE' as const}
    render(<SourceDetails grounding={g} />)
    expect(screen.queryByText('Nguồn tham chiếu')).not.toBeInTheDocument()
    expect(screen.getByText('Không có nguồn đính kèm')).toBeVisible()
    expect(screen.getByText('Dữ liệu mô phỏng, không phải hướng dẫn thực tế.')).toBeVisible()
  })
  it('distinguishes an outside request from missing evidence', () => {
    render(<SourceDetails grounding={grounding('INSUFFICIENT_DATA')} resultStatus="out_of_scope" />)
    expect(screen.getByText('Ngoài phạm vi')).toBeVisible()
    expect(screen.queryByText('Chưa đủ dữ liệu')).not.toBeInTheDocument()
  })
})
