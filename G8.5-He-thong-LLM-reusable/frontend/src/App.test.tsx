import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import App, { FeedbackPanel } from './App'
import * as api from './services/api'
import type { Conversation, CreateMessageResponse, Grounding, Message, User } from './types/api'

// Compatibility tests deliberately exercise account mode and a checklist-capable UI.
vi.hoisted(() => {
  vi.stubEnv('VITE_VISITOR_MODE', 'false')
  vi.stubEnv('VITE_REQUIRE_ACCOUNT', 'false')
})

vi.mock('./services/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./services/api')>()
  return {
    ...actual,
    getDomain: vi.fn(),
    createConversation: vi.fn(),
    streamMessage: vi.fn(),
    watchJob: vi.fn(),
    getActiveJob: vi.fn(),
    getAccounts: vi.fn(),
    startGuest: vi.fn(),
    passwordLogin: vi.fn(),
    register: vi.fn(),
    deleteMessageFeedback: vi.fn(),
    getConversations: vi.fn(),
    getConversationFeedback: vi.fn(),
    getCurrentUser: vi.fn(),
    getMessages: vi.fn(),
    login: vi.fn(),
    logout: vi.fn(),
    putMessageFeedback: vi.fn(),
  }
})

const currentUser: User = { id: 'user-1', display_name: 'Tú' }
const conversationA: Conversation = {
  id: 'conversation-a',
  title: 'Hồ sơ A',
  created_at: '2026-09-08T02:00:00Z',
  updated_at: '2026-09-08T02:00:00Z',
}
const conversationB: Conversation = {
  id: 'conversation-b',
  title: 'Hồ sơ B',
  created_at: '2026-09-08T03:00:00Z',
  updated_at: '2026-09-08T03:00:00Z',
}

const getAccountsMock = vi.mocked(api.getAccounts)
const getConversationsMock = vi.mocked(api.getConversations)
const getMessagesMock = vi.mocked(api.getMessages)
const createMessageMock = vi.mocked(api.streamMessage)
const loginMock = vi.mocked(api.passwordLogin)
const putMessageFeedbackMock = vi.mocked(api.putMessageFeedback)
const deleteMessageFeedbackMock = vi.mocked(api.deleteMessageFeedback)

function message(
  id: string,
  conversationId: string,
  role: Message['role'],
  content: string,
  status: Message['status'] = 'completed',
): Message {
  return {
    id,
    conversation_id: conversationId,
    role,
    content,
    status,
    created_at: '2026-09-08T04:00:00Z',
  }
}

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

describe('G6 account workspace', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    vi.mocked(api.getDomain).mockResolvedValue({title:'Thủ tục nào bạn đang cần?', version:'test',
      capabilities:{checklist:true}, attributes:[], suggestions:[]})
    vi.stubGlobal('matchMedia', vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }))
    getAccountsMock.mockResolvedValue([currentUser])
    getConversationsMock.mockResolvedValue([])
    getMessagesMock.mockResolvedValue([])
    vi.mocked(api.getActiveJob).mockResolvedValue(null)
    vi.mocked(api.getConversationFeedback).mockResolvedValue([])
    vi.mocked(api.startGuest).mockResolvedValue({ ...currentUser, is_guest: true })
    vi.mocked(api.createConversation).mockResolvedValue(conversationA)
    loginMock.mockResolvedValue(currentUser)
  })

  it('opens directly on composer without creating an empty conversation', async () => {
    getAccountsMock.mockResolvedValue([])
    render(<App />)
    await waitFor(() => expect(screen.getByLabelText('Câu hỏi của bạn')).toBeEnabled())
    expect(screen.getByRole('heading', { name: 'Thủ tục nào bạn đang cần?' })).toBeInTheDocument()
    expect(api.createConversation).not.toHaveBeenCalled()
    expect(screen.queryByText(/Google/)).not.toBeInTheDocument()
  })

  it('logs in using username and password, never display name alone', async () => {
    getAccountsMock.mockResolvedValue([])
    const user = userEvent.setup()
    render(<App />)
    await user.click(await screen.findByRole('button', { name: 'Đăng nhập' }))
    await user.type(screen.getByLabelText('Tên đăng nhập'), 'HOAN')
    await user.type(screen.getByLabelText('Mật khẩu', { exact: true }), 'Safe-pass-123')
    await user.click(screen.getByRole('button', { name: 'Đăng nhập' }))
    await waitFor(() => expect(loginMock).toHaveBeenCalledWith('hoan', 'Safe-pass-123'))
  })

  it('rejects non-matching confirmation before submitting registration', async () => {
    getAccountsMock.mockResolvedValue([])
    const user = userEvent.setup()
    render(<App />)
    await user.click(await screen.findByRole('button', { name: 'Đăng nhập' }))
    await user.click(screen.getByRole('button', { name: 'Đăng ký' }))
    await user.type(screen.getByLabelText('Họ và tên'), 'Test')
    await user.type(screen.getByLabelText('Tên đăng nhập'), 'tester')
    await user.type(screen.getByLabelText('Mật khẩu', { exact: true }), 'Safe-pass-123')
    await user.type(screen.getByLabelText('Xác nhận mật khẩu'), 'Different-123')
    await user.click(screen.getByRole('button', { name: 'Tạo tài khoản' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('chưa khớp')
    expect(api.register).not.toHaveBeenCalled()
  })

  it('creates guest and conversation on first question, prevents duplicate sends', async () => {
    getAccountsMock.mockResolvedValue([])
    const generation = deferred<CreateMessageResponse>()
    vi.mocked(api.startGuest).mockImplementation(async () => {
      getAccountsMock.mockResolvedValue([{ ...currentUser, is_guest: true }])
      return { ...currentUser, is_guest: true }
    })
    createMessageMock.mockReturnValue(generation.promise)
    const user = userEvent.setup()
    render(<App />)
    await waitFor(() => expect(screen.getByLabelText('Câu hỏi của bạn')).toBeEnabled())
    await user.type(screen.getByLabelText('Câu hỏi của bạn'), 'Xin chào{Enter}')
    await waitFor(() => expect(createMessageMock).toHaveBeenCalledOnce())
    expect(api.startGuest).toHaveBeenCalledOnce()
    expect(api.createConversation).toHaveBeenCalledOnce()
    expect(screen.getByRole('textbox', { name: 'Câu hỏi của bạn' })).toBeDisabled()
    await act(async () => generation.resolve({
      user_message: message('u1', conversationA.id, 'user', 'Xin chào'),
      assistant_message: message('a1', conversationA.id, 'assistant', 'Trả lời có kiểm chứng'),
    }))
    expect(await screen.findByText('Trả lời có kiểm chứng')).toBeInTheDocument()
    expect(screen.getAllByText('Xin chào')).toHaveLength(1)
  })

  it('retries failed delivery with the same client id', async () => {
    createMessageMock.mockRejectedValueOnce(new Error('Mất kết nối')).mockResolvedValueOnce({
      user_message: message('u1', conversationA.id, 'user', 'Giấy tờ?'),
      assistant_message: message('a1', conversationA.id, 'assistant', 'Trả lời'),
    })
    const user = userEvent.setup()
    render(<App />)
    await waitFor(() => expect(screen.getByLabelText('Câu hỏi của bạn')).toBeEnabled())
    await user.type(screen.getByLabelText('Câu hỏi của bạn'), 'Giấy tờ?{Enter}')
    await user.click(await screen.findByRole('button', { name: 'Thử lại' }))
    expect(await screen.findByText('Trả lời')).toBeInTheDocument()
    expect(createMessageMock.mock.calls[0][1].client_message_id).toBe(createMessageMock.mock.calls[1][1].client_message_id)
    expect(screen.getAllByText('Giấy tờ?')).toHaveLength(1)
  })

  it('ignores late history from a previously selected conversation', async () => {
    getConversationsMock.mockResolvedValue([conversationA, conversationB])
    const historyA = deferred<Message[]>()
    getMessagesMock.mockImplementation((id) => id === conversationA.id ? historyA.promise : Promise.resolve([message('b1', conversationB.id, 'assistant', 'Nội dung B')]))
    const user = userEvent.setup()
    render(<App />)
    await user.click(await screen.findByRole('button', { name: 'Hồ sơ A' }))
    await user.click(screen.getByRole('button', { name: 'Hồ sơ B' }))
    expect(await screen.findByText('Nội dung B')).toBeInTheDocument()
    await act(async () => historyA.resolve([message('a1', conversationA.id, 'assistant', 'Nội dung A')]))
    expect(screen.queryByText('Nội dung A')).not.toBeInTheDocument()
  })

  it('clears private data after a session expires', async () => {
    getConversationsMock.mockResolvedValue([conversationA])
    getMessagesMock.mockRejectedValueOnce(new api.ApiError('Hết phiên.', 401, 'UNAUTHORIZED'))
    const user = userEvent.setup()
    render(<App />)
    await user.click(await screen.findByRole('button', { name: 'Hồ sơ A' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Hết phiên')
    expect(screen.queryByText('Hồ sơ A')).not.toBeInTheDocument()
    expect(sessionStorage.getItem('hcc-account-id')).toBeNull()
  })

  it('reconnects to a persisted active job after reopening the thread', async () => {
    getConversationsMock.mockResolvedValue([conversationA])
    vi.mocked(api.getActiveJob).mockResolvedValue({ job_id: 'j1', conversation_id: conversationA.id, state: 'running' })
    vi.mocked(api.watchJob).mockResolvedValue({
      user_message: message('u1', conversationA.id, 'user', 'Câu hỏi đã lưu'),
      assistant_message: message('a1', conversationA.id, 'assistant', 'Kết quả đã lưu'),
    })
    const user = userEvent.setup()
    render(<App />)
    await user.click(await screen.findByRole('button', { name: 'Hồ sơ A' }))
    expect(await screen.findByText('Kết quả đã lưu')).toBeInTheDocument()
    expect(api.streamMessage).not.toHaveBeenCalled()
  })

  it.each([false, true])('renders sources and G4 extraction state (fallback=%s)', async (fallback) => {
    getConversationsMock.mockResolvedValue([conversationA])
    const grounding: Grounding = {
      request_id: '00000000-0000-4000-8000-000000000001',
      evidence_bundle_id: '10000000-0000-4000-8000-000000000001',
      status: 'ANSWER',
      corpus_version: 'fixture-v1',
      data_classification: 'D0_SYSTEM_SMOKE',
      prompt_version: 'g2-grounded-v2',
      provider: 'template',
      model: 'template-grounded-v2',
      missing_information: [],
      verification: {
        plan_version: 'g4-answer-plan-v1',
        policy: 'full-claim-exact-v1',
        candidate_checked: true,
        candidate_passed: !fallback,
        fallback_used: fallback,
        reasons: fallback ? ['FULL_CLAIM_TEXT_NOT_VERIFIED'] : [],
      },
      checklist: [
        {
          field: 'fees',
          label: 'Lệ phí',
          value: 'Không thu phí.',
          evidence_ids: ['frag_fees'],
        },
      ],
      knowledge_version: 'g5-source-v000001',
      source_checked_at: '2026-09-12T01:00:00Z',
      sources: [
        {
          fragment_id: 'frag_fees',
          source_id: 'src_synthetic',
          title: 'Nguồn fixture chính thức',
          url: 'https://fixture.invalid/synthetic',
          metadata: {},
        },
        {
          fragment_id: 'frag_bad',
          source_id: 'src_bad',
          title: 'Nguồn không an toàn',
          url: 'javascript:alert(1)',
          metadata: {},
        },
      ],
    }
    getMessagesMock.mockResolvedValue([
      message('u-1', conversationA.id, 'user', 'Lệ phí?'),
      { ...message('a-1', conversationA.id, 'assistant', 'Câu trả lời có nguồn'), grounding },
    ])
    const user = userEvent.setup()

    render(<App />)
    await user.click(await screen.findByRole('button', { name: 'Hồ sơ A' }))

    await user.click(await screen.findByText('Nguồn tham chiếu'))
    await user.click(screen.getByText('Chi tiết đối chiếu'))
    expect(screen.getByText(/Dữ liệu mô phỏng/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Nguồn fixture chính thức' })).toHaveAttribute(
      'href',
      'https://fixture.invalid/synthetic',
    )
    expect(screen.getByText('Nguồn không an toàn').closest('a')).toBeNull()
    expect(screen.getByText('g5-source-v000001')).toBeInTheDocument()
    expect(screen.getByText('Lệ phí').tagName).toBe('DT')
    expect(screen.getByText('Không thu phí.').tagName).toBe('DD')
    expect(screen.getByText(fallback
      ? /Hệ thống dùng nội dung trích xuất/
      : /Nội dung đã qua bước đối chiếu/)).toBeInTheDocument()
  })

  it('collects two independent feedback axes and an optional reason', async () => {
    const saved = {
      id: 'feedback-1',
      message_id: 'assistant-1',
      relevance: 'relevant' as const,
      satisfaction: 'not_satisfied' as const,
      reason: 'Thiếu chi tiết',
      created_at: '2026-09-13T01:00:00Z',
      updated_at: '2026-09-13T01:00:00Z',
    }
    putMessageFeedbackMock.mockResolvedValue(saved)
    const onChange = vi.fn()
    const onError = vi.fn()
    const user = userEvent.setup()

    render(
      <FeedbackPanel
        messageId="assistant-1"
        onChange={onChange}
        onError={onError}
      />,
    )
    await user.click(screen.getByRole('button', { name: /^Phù hợp$/ }))
    await user.click(screen.getByRole('button', { name: 'Chưa hài lòng' }))
    await user.type(screen.getByLabelText('Lý do phản hồi'), 'Thiếu chi tiết')
    await user.click(screen.getByRole('button', { name: 'Lưu phản hồi' }))

    expect(putMessageFeedbackMock).toHaveBeenCalledWith('assistant-1', {
      relevance: 'relevant',
      satisfaction: 'not_satisfied',
      reason: 'Thiếu chi tiết',
    })
    expect(onChange).toHaveBeenCalledWith(saved)
    expect(onError).not.toHaveBeenCalled()
  })

  it('explains incomplete feedback instead of leaving a silent disabled save button', async () => {
    const user = userEvent.setup()
    render(<FeedbackPanel messageId="assistant-1" onChange={vi.fn()} onError={vi.fn()} />)
    await user.click(screen.getByRole('button', { name: /^Không phù hợp$/ }))
    await user.type(screen.getByLabelText('Lý do phản hồi'), 'chưa phù hợp')
    await user.click(screen.getByRole('button', { name: 'Lưu phản hồi' }))
    expect(screen.getByRole('alert')).toHaveTextContent('chưa chọn mức độ hài lòng')
    expect(putMessageFeedbackMock).not.toHaveBeenCalled()
  })

  it('allows the owner to delete an existing feedback', async () => {
    deleteMessageFeedbackMock.mockResolvedValue(undefined)
    const onChange = vi.fn()
    const user = userEvent.setup()
    render(
      <FeedbackPanel
        messageId="assistant-1"
        initial={{
          id: 'feedback-1',
          message_id: 'assistant-1',
          relevance: 'not_relevant',
          satisfaction: 'not_satisfied',
          created_at: '2026-09-13T01:00:00Z',
          updated_at: '2026-09-13T01:00:00Z',
        }}
        onChange={onChange}
        onError={vi.fn()}
      />,
    )

    await user.click(screen.getByRole('button', { name: 'Xóa phản hồi' }))
    expect(deleteMessageFeedbackMock).toHaveBeenCalledWith('assistant-1')
    expect(onChange).toHaveBeenCalledWith(null)
  })

  it('renders a private reviewed D2 source without inventing a public link', async () => {
    getConversationsMock.mockResolvedValue([conversationA])
    const grounding: Grounding = {
      request_id: '00000000-0000-4000-8000-000000000002',
      evidence_bundle_id: '10000000-0000-4000-8000-000000000002',
      status: 'ANSWER',
      corpus_version: 'private-demo-v1',
      data_classification: 'D2_COMPANY_REAL',
      prompt_version: 'g2-grounded-v2',
      provider: 'local',
      model: 'Qwen3-1.7B',
      missing_information: [],
      sources: [
        {
          fragment_id: 'frag_private',
          source_id: 'src_private',
          title: 'Nguồn nội bộ đã duyệt',
          url: null,
          metadata: {},
        },
      ],
    }
    getMessagesMock.mockResolvedValue([
      message('u-2', conversationA.id, 'user', 'Câu hỏi thử'),
      { ...message('a-2', conversationA.id, 'assistant', 'Câu trả lời thử'), grounding },
    ])
    const user = userEvent.setup()

    render(<App />)
    await user.click(await screen.findByRole('button', { name: 'Hồ sơ A' }))

    await user.click(await screen.findByText('Nguồn tham chiếu'))
    expect(screen.queryByText(/Nguồn nội bộ đã duyệt cho demo\./)).not.toBeInTheDocument()
    expect(screen.getByText(/^Nguồn:/)).toBeInTheDocument()
    expect(screen.getByText('Nguồn nội bộ đã duyệt').closest('a')).toBeNull()
  })
})
