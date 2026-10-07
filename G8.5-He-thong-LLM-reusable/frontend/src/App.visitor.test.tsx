import { render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import App from './App'
import * as api from './services/api'

vi.hoisted(() => {
  vi.stubEnv('VITE_VISITOR_MODE', 'true')
  vi.stubEnv('VITE_REQUIRE_ACCOUNT', 'false')
})

vi.mock('./services/api', async (importOriginal) => ({
  ...await importOriginal<typeof import('./services/api')>(),
  getDomain: vi.fn().mockResolvedValue({title:'Tài liệu giả lập',version:'test',
    capabilities:{checklist:false},attributes:[],suggestions:[]}),
  getAccounts: vi.fn(), startGuest: vi.fn(), renewBrowserSession: vi.fn(),
  getConversations: vi.fn(), getMessages: vi.fn(), getActiveJob: vi.fn(),
  getConversationFeedback: vi.fn(), createConversation: vi.fn(),
}))

const guest = {id:'visitor-1',display_name:'Khách',is_guest:true}
const conversation = {id:'thread-1',title:'Hội thoại đã lưu',
  created_at:'2026-10-07T00:00:00Z',updated_at:'2026-10-07T00:00:00Z'}

describe('Cookie visitor workspace', () => {
  beforeEach(() => {
    localStorage.clear()
    vi.clearAllMocks()
    vi.stubGlobal('matchMedia', vi.fn().mockReturnValue({matches:false,
      addEventListener:vi.fn(),removeEventListener:vi.fn()}))
    vi.mocked(api.getAccounts).mockResolvedValue([])
    vi.mocked(api.startGuest).mockResolvedValue(guest)
    vi.mocked(api.renewBrowserSession).mockResolvedValue(guest)
    vi.mocked(api.getConversations).mockResolvedValue([])
    vi.mocked(api.getMessages).mockResolvedValue([])
    vi.mocked(api.getActiveJob).mockResolvedValue(null)
    vi.mocked(api.getConversationFeedback).mockResolvedValue([])
  })

  it('creates a visitor automatically without a login dialog or an empty thread', async () => {
    render(<App />)
    await waitFor(() => expect(screen.getByLabelText('Câu hỏi của bạn')).toBeEnabled())
    expect(api.startGuest).toHaveBeenCalledTimes(1)
    expect(api.createConversation).not.toHaveBeenCalled()
    expect(screen.queryByRole('button',{name:'Đăng nhập'})).not.toBeInTheDocument()
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('uses and renews the server-authenticated cookie identity instead of creating a new visitor', async () => {
    vi.mocked(api.getAccounts).mockResolvedValue([guest])
    render(<App />)
    await waitFor(() => expect(screen.getByLabelText('Câu hỏi của bạn')).toBeEnabled())
    expect(api.renewBrowserSession).toHaveBeenCalledTimes(1)
    expect(api.startGuest).not.toHaveBeenCalled()
  })

  it('restores latest history when only cookies survive browser restart', async () => {
    vi.mocked(api.getAccounts).mockResolvedValue([guest])
    vi.mocked(api.getConversations).mockResolvedValue([conversation])
    render(<App />)
    await waitFor(() => expect(api.getMessages).toHaveBeenCalledWith(conversation.id))
    expect(await screen.findByRole('button',{name:conversation.title})).toBeInTheDocument()
  })

  it('preserves an explicitly blank new chat instead of selecting older history', async () => {
    vi.mocked(api.getAccounts).mockResolvedValue([guest])
    vi.mocked(api.getConversations).mockResolvedValue([conversation])
    localStorage.setItem('hcc-conversation:' + guest.id, '')
    render(<App />)
    await waitFor(() => expect(screen.getByLabelText('Câu hỏi của bạn')).toBeEnabled())
    expect(api.getMessages).not.toHaveBeenCalled()
  })
})
