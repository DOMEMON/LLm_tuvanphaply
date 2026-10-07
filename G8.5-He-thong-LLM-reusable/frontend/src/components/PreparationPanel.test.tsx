import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, expect, it, vi } from 'vitest'
import { PreparationPanel } from './PreparationPanel'
import * as api from '../services/api'
import type { PreparationChecklist } from '../types/api'

vi.mock('../services/api', async (original) => ({
  ...await original<typeof import('../services/api')>(),
  getChecklists: vi.fn(), createChecklist: vi.fn(), changeChecklistItem: vi.fn(),
  resetChecklist: vi.fn(), deleteChecklist: vi.fn(),
}))
const row: PreparationChecklist = {
  id: 'list-1', title: 'Thủ tục thử nghiệm', procedure_id: 'p', conversation_id: 'c',
  corpus_version: 'v1', source_changed: false, revision: 1, created_at: '', updated_at: '',
  guidance: 'Trạng thái do bạn cung cấp, chưa xác nhận hồ sơ đầy đủ hoặc hợp lệ.',
  counts: { have: 0, missing: 0, unknown: 1, not_applicable: 0 },
  items: [{ id: 'item-1', text: 'Giấy tờ (nếu có)', section: 'Đối với trường hợp A:',
    evidence_ids: ['p:001'], status: 'unknown', status_source: 'NOT_PROVIDED' }],
}
beforeEach(() => {
  vi.resetAllMocks()
  vi.mocked(api.createChecklist).mockResolvedValue(row)
  vi.mocked(api.getChecklists).mockResolvedValue([row])
  vi.stubEnv('VITE_API_BASE_URL', 'http://localhost:8006/api/v1')
})
it('creates from a source message, keeps conditions and sends explicit revision', async () => {
  const changed = { ...row, revision: 2, counts: { ...row.counts, unknown: 0, have: 1 },
    items: [{ ...row.items[0], status: 'have' as const, status_source: 'USER_REPORTED' as const }] }
  vi.mocked(api.changeChecklistItem).mockResolvedValue(changed)
  render(<PreparationPanel messageId="message-1" onClose={() => {}} />)
  expect(await screen.findByText('Giấy tờ (nếu có)')).toBeInTheDocument()
  expect(api.createChecklist).toHaveBeenCalledWith('message-1')
  await userEvent.selectOptions(screen.getByRole('combobox'), 'have')
  expect(api.changeChecklistItem).toHaveBeenCalledWith('list-1', 'item-1', 1, 'have')
  expect(await screen.findByText('Bạn tự xác nhận')).toBeInTheDocument()
})
it('refreshes after stale revision instead of overwriting another tab', async () => {
  vi.mocked(api.changeChecklistItem).mockRejectedValue(new api.ApiError('Đã đổi ở tab khác', 409, 'CHECKLIST_CONFLICT'))
  render(<PreparationPanel messageId="message-1" onClose={() => {}} />)
  await screen.findByRole('combobox')
  await userEvent.selectOptions(screen.getByRole('combobox'), 'missing')
  expect(await screen.findByRole('alert')).toHaveTextContent('Đã đổi ở tab khác')
  expect(api.getChecklists).toHaveBeenCalledTimes(2)
})
it('requires confirmation before deleting progress', async () => {
  render(<PreparationPanel messageId="message-1" onClose={() => {}} />)
  await userEvent.click(await screen.findByRole('button', { name: 'Xóa checklist' }))
  expect(api.deleteChecklist).not.toHaveBeenCalled()
  await userEvent.click(screen.getByRole('button', { name: 'Xác nhận' }))
  expect(api.deleteChecklist).toHaveBeenCalledWith('list-1', 1)
  expect(await screen.findByText(/Chưa có checklist/)).toBeInTheDocument()
})

const form = {
  id: 'reviewed-form', title: 'Đơn đề nghị đã đối chiếu', form_number: 'Mẫu số 01',
  legal_basis: 'Văn bản gốc', source_url: 'https://chinhphu.vn/?docid=123',
  issuing_authority: 'Chính phủ', jurisdiction: 'Việt Nam', applicability: 'Không thay giấy đã cấp.',
  extraction_note: 'PDF trích nguyên trang 7–8.', checked_at: '2026-09-27T00:00:00+07:00',
  review_due_at: '2026-10-05T00:00:00+07:00', bytes: 100,
}
it('shows a local reviewed download only after missing, and hides it again on have', async () => {
  const missing: PreparationChecklist = { ...row, revision: 2,
    items: [{ ...row.items[0], status: 'missing', forms: [form] }] }
  vi.mocked(api.changeChecklistItem).mockResolvedValueOnce(missing).mockResolvedValueOnce({
    ...row, revision: 3, items: [{ ...row.items[0], status: 'have', forms: [] }],
  })
  render(<PreparationPanel messageId="message-1" onClose={() => {}} />)
  await screen.findByRole('combobox')
  expect(screen.queryByRole('link', { name: 'Tải mẫu PDF' })).not.toBeInTheDocument()
  await userEvent.selectOptions(screen.getByRole('combobox'), 'missing')
  expect(await screen.findByRole('link', { name: 'Tải mẫu PDF' })).toHaveAttribute('href',
    'http://localhost:8006/api/v1/forms/reviewed-form/download')
  expect(screen.getByRole('link', { name: 'Đối chiếu nguồn gốc' })).toHaveAttribute('href', form.source_url)
  expect(screen.getByText('Không thay giấy đã cấp.')).toBeInTheDocument()
  expect(screen.getByText(/Tải mẫu không có nghĩa/)).toBeInTheDocument()
  await userEvent.selectOptions(screen.getByRole('combobox'), 'have')
  expect(screen.queryByRole('link', { name: 'Tải mẫu PDF' })).not.toBeInTheDocument()
})
it('leaves a missing item blank when no reviewed form exists', async () => {
  const missing: PreparationChecklist = { ...row, items: [{ ...row.items[0], status: 'missing', forms: [] }] }
  vi.mocked(api.getChecklists).mockResolvedValue([missing])
  render(<PreparationPanel messageId="message-1" onClose={() => {}} />)
  await screen.findByRole('combobox')
  expect(screen.queryByRole('link')).not.toBeInTheDocument()
  expect(screen.queryByRole('alert')).not.toBeInTheDocument()
})
