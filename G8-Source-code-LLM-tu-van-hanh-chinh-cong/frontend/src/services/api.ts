import type {
  Conversation,
  CreateMessageInput,
  CreateMessageResponse,
  FeedbackInput,
  Message,
  MessageFeedback,
  User,
  AuthConfig,
  Registration,
  PreparationChecklist,
  PreparationStatus,
} from '../types/api'

type ErrorResponse = {
  error?: {
    code?: string
    message?: string
    request_id?: string
  }
}

export class ApiError extends Error {
  readonly status: number
  readonly code?: string
  readonly requestId?: string

  constructor(message: string, status: number, code?: string, requestId?: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.requestId = requestId
  }
}

const LOOPBACK_HOSTS = new Set(['localhost', '127.0.0.1', '::1'])

export function resolveApiBaseUrl(configuredValue: string, pageUrl: string) {
  const page = new URL(pageUrl)
  // A share build uses /api/v1 behind its HTTPS gateway. Absolute URLs used
  // by existing local/other-stage builds keep their original behavior.
  const configured = new URL(configuredValue, page.origin)
  if (
    LOOPBACK_HOSTS.has(configured.hostname) &&
    LOOPBACK_HOSTS.has(page.hostname) &&
    configured.hostname !== page.hostname
  ) {
    configured.hostname = page.hostname
  }
  return configured.toString().replace(/\/$/, '')
}

export function getApiBaseUrl() {
  const apiBaseUrl = import.meta.env.VITE_API_BASE_URL?.trim().replace(/\/$/, '')
  if (!apiBaseUrl) {
    throw new Error('Thiếu VITE_API_BASE_URL. Hãy cấu hình URL của Core Backend.')
  }
  return resolveApiBaseUrl(apiBaseUrl, window.location.href)
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
    const headers = new Headers(options.headers)
    const account = sessionStorage.getItem('hcc-account-id')
    if (account) headers.set('X-Account-ID', account)
  if (options.body !== undefined && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json')
  }

  let response: Response
  try {
    response = await fetch(`${getApiBaseUrl()}${path}`, {
      ...options,
      credentials: 'include',
      headers,
    })
  } catch (requestError) {
    if (requestError instanceof DOMException && requestError.name === 'AbortError') throw requestError
    throw new Error(
      'Không kết nối được Core Backend. Hãy kiểm tra backend và cấu hình CORS.',
      { cause: requestError },
    )
  }

  if (!response.ok) {
    const body = (await response.json().catch(() => null)) as ErrorResponse | null
    throw new ApiError(
      body?.error?.message ?? 'Core Backend trả về lỗi.',
      response.status,
      body?.error?.code,
      body?.error?.request_id,
    )
  }

  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
}

const conversationPath = (conversationId: string) =>
  `/conversations/${encodeURIComponent(conversationId)}`

export const getCurrentUser = () => request<User>('/auth/me')
export const login = (displayName: string) =>
  request<User>('/auth/login', {
    method: 'POST',
    body: JSON.stringify({ display_name: displayName }),
  })
export const logout = () => request<void>('/auth/logout', { method: 'POST' })
export const getConversations = () => request<Conversation[]>('/conversations')
export const createConversation = () =>
  request<Conversation>('/conversations', { method: 'POST', body: JSON.stringify({}) })
export const getMessages = (conversationId: string, signal?: AbortSignal) =>
  request<Message[]>(`${conversationPath(conversationId)}/messages`, { signal })
export const createMessage = (conversationId: string, input: CreateMessageInput) =>
  request<CreateMessageResponse>(`${conversationPath(conversationId)}/messages`, {
    method: 'POST',
    body: JSON.stringify(input),
  })
export const getConversationFeedback = (conversationId: string, signal?: AbortSignal) =>
  request<MessageFeedback[]>(`${conversationPath(conversationId)}/feedback`, { signal })
export const putMessageFeedback = (messageId: string, input: FeedbackInput) =>
  request<MessageFeedback>(`/messages/${encodeURIComponent(messageId)}/feedback`, {
    method: 'PUT',
    body: JSON.stringify(input),
  })
export const deleteMessageFeedback = (messageId: string) =>
  request<void>(`/messages/${encodeURIComponent(messageId)}/feedback`, { method: 'DELETE' })

export const selectAccount = (id: string | null) => {
  if (id) sessionStorage.setItem('hcc-account-id', id)
  else sessionStorage.removeItem('hcc-account-id')
}
export const getAccounts = () => request<User[]>('/auth/accounts')
export const getAuthConfig = () => request<AuthConfig>('/auth/config')
export const passwordLogin = (username: string, password: string) =>
  request<User>('/auth/password', { method: 'POST', body: JSON.stringify({ username, password }) })
export const register = (input: Registration) =>
  request<User>('/auth/register', { method: 'POST', body: JSON.stringify(input) })
export const startGuest = () => request<User>('/auth/guest', { method: 'POST' })
export const changePassword = (input: { username: string; password: string; password_confirmation: string; current_password?: string }) =>
  request<User>('/auth/password', { method: 'PUT', body: JSON.stringify(input) })
export const updateConversation = (id: string, input: { title?: string; is_pinned?: boolean }) =>
  request<Conversation>(conversationPath(id), { method: 'PATCH', body: JSON.stringify(input) })
export const deleteConversation = (id: string) => request<void>(conversationPath(id), { method: 'DELETE' })

export const getChecklists = () => request<PreparationChecklist[]>('/checklists')
export const getFormDownloadUrl = (id: string) => `${getApiBaseUrl()}/forms/${encodeURIComponent(id)}/download`
export const createChecklist = (messageId: string, procedureId?: string) => request<PreparationChecklist>('/checklists', { method: 'POST', body: JSON.stringify({ message_id: messageId, ...(procedureId ? { procedure_id: procedureId } : {}) }) })
export const changeChecklistItem = (id: string, itemId: string, revision: number, status: PreparationStatus) => request<PreparationChecklist>(`/checklists/${encodeURIComponent(id)}/items/${encodeURIComponent(itemId)}`, { method: 'PATCH', body: JSON.stringify({ revision, status }) })
export const resetChecklist = (id: string, revision: number) => request<PreparationChecklist>(`/checklists/${encodeURIComponent(id)}/reset`, { method: 'POST', body: JSON.stringify({ revision }) })
export const deleteChecklist = (id: string, revision: number) => request<void>(`/checklists/${encodeURIComponent(id)}?revision=${revision}`, { method: 'DELETE' })

export async function streamMessageSSE(
  conversationId: string,
  input: CreateMessageInput,
  onEvent: (event: { type: string; text?: string; message?: string }) => void,
): Promise<CreateMessageResponse> {
  const headers = new Headers({ 'Content-Type': 'application/json' })
  const account = sessionStorage.getItem('hcc-account-id')
  if (account) headers.set('X-Account-ID', account)
  const response = await fetch(`${getApiBaseUrl()}${conversationPath(conversationId)}/messages/stream`, {
    method: 'POST', credentials: 'include', headers, body: JSON.stringify(input),
  })
  if (!response.ok) {
    const body = await response.json().catch(() => null) as ErrorResponse | null
    throw new ApiError(body?.error?.message ?? 'Chưa gửi được câu hỏi.', response.status, body?.error?.code)
  }
  if (!response.body) throw new Error('Trình duyệt không hỗ trợ nhận câu trả lời trực tiếp.')
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let result: CreateMessageResponse | null = null
  try {
    while (true) {
      const { value, done } = await reader.read()
      buffer += decoder.decode(value, { stream: !done }).replace(/\r\n/g, '\n')
      let end: number
      while ((end = buffer.indexOf('\n\n')) >= 0) {
        const packet = buffer.slice(0, end)
        buffer = buffer.slice(end + 2)
        const type = packet.split('\n').find((line) => line.startsWith('event:'))?.slice(6).trim()
        const data = packet.split('\n').filter((line) => line.startsWith('data:')).map((line) => line.slice(5).trim()).join('\n')
        if (!type || !data) continue
        const payload = JSON.parse(data)
        if (type === 'error') throw new ApiError(payload.message, payload.status ?? 500, payload.code)
        if (type === 'complete') result = payload as CreateMessageResponse
        else onEvent({ type, ...payload })
      }
      if (done) break
    }
  } finally { await reader.cancel().catch(() => undefined); reader.releaseLock() }
  if (!result) throw new Error('Kết nối bị gián đoạn. Nhấn thử lại để nhận câu trả lời đã lưu.')
  return result
}

export type JobState = {
  job_id: string; conversation_id: string; state: 'queued' | 'running' | 'completed' | 'failed'
  position?: number; message?: string; result?: CreateMessageResponse; poll_after_ms?: number
  client_message_id?: string; content?: string
  error?: { code: string; message: string; status: number }
}
type Progress = (event: { type: string; message?: string; text?: string }) => void
export const getActiveJob = (id: string) => request<JobState | null>(`${conversationPath(id)}/active-job`)

export async function watchJob(job: JobState, onEvent: Progress): Promise<CreateMessageResponse> {
  const finish = (state: JobState) => {
    if (state.job_id !== job.job_id) throw new Error('Phản hồi không khớp với yêu cầu.')
    if (state.state === 'failed') throw new ApiError(state.error?.message ?? 'Xử lý chưa thành công.', state.error?.status ?? 500, state.error?.code)
    if (state.state === 'completed' && state.result) return state.result
    onEvent({ type: 'status', message: state.message })
    return null
  }
  const immediate = finish(job)
  if (immediate) return immediate
  const url = new URL(`${getApiBaseUrl()}/jobs/${encodeURIComponent(job.job_id)}/socket`)
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:'
  url.searchParams.set('account_id', sessionStorage.getItem('hcc-account-id') ?? '')
  // A socket is scoped to one authorized job; never subscribe to a global answer channel.
  const completed = await new Promise<CreateMessageResponse | null>((resolve, reject) => {
    let socket: WebSocket
    try { socket = new WebSocket(url) } catch { resolve(null); return }
    let ended = false
    const cleanup = () => { ended = true; clearTimeout(timer); socket.close() }
    let timer = window.setTimeout(() => { cleanup(); resolve(null) }, 10000)
    socket.onmessage = (event) => {
      try {
        const result = finish(JSON.parse(event.data) as JobState)
        if (result) { cleanup(); resolve(result) }
        else { clearTimeout(timer); timer = window.setTimeout(() => { cleanup(); resolve(null) }, 10000) }
      } catch (error) { cleanup(); reject(error) }
    }
    socket.onerror = socket.onclose = () => { if (!ended) { cleanup(); resolve(null) } }
  })
  if (completed) return completed
  // Restricted proxies may block WebSockets; the same durable job remains available over HTTP.
  const deadline = Date.now() + 360000
  while (Date.now() < deadline) {
    const state = await request<JobState>(`/jobs/${encodeURIComponent(job.job_id)}`)
    const result = finish(state)
    if (result) return result
    const delay = Number.isFinite(state.poll_after_ms)
      ? Math.min(5000, Math.max(500, state.poll_after_ms!)) : 1000
    await new Promise((resolve) => setTimeout(resolve, delay))
  }
  throw new Error('Kết nối chờ quá lâu. Yêu cầu đã lưu; hãy mở lại hội thoại để kiểm tra.')
}

export async function streamMessage(conversationId: string, input: CreateMessageInput, onEvent: Progress) {
  try {
    const job = await request<JobState>(`${conversationPath(conversationId)}/jobs`, { method: 'POST', body: JSON.stringify(input) })
    return await watchJob(job, onEvent)
  } catch (error) {
    // Older stage configurations intentionally disable the queue. Never bypass a full queue.
    if (error instanceof ApiError && error.code === 'QUEUE_DISABLED') return streamMessageSSE(conversationId, input, onEvent)
    throw error
  }
}

export const renewBrowserSession = () => request<User>('/auth/browser-session', { method: 'POST' })
