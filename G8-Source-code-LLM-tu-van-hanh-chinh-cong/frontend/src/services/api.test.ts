import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError, getCurrentUser, login, putMessageFeedback, resolveApiBaseUrl, selectAccount, streamMessage, streamMessageSSE, watchJob } from './api'

describe('Core API client', () => {
  const fetchMock = vi.fn()

  beforeEach(() => {
    vi.stubEnv('VITE_API_BASE_URL', 'http://localhost:8000/api/v1/')
    vi.stubGlobal('fetch', fetchMock)
  })

  afterEach(() => {
    fetchMock.mockReset()
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  it('keeps the API cookie on the same loopback hostname as the opened page', () => {
    expect(
      resolveApiBaseUrl('http://localhost:8005/api/v1/', 'http://127.0.0.1:3005/'),
    ).toBe('http://127.0.0.1:8005/api/v1')
    expect(
      resolveApiBaseUrl('https://api.example.vn/api/v1/', 'https://portal.example.vn/'),
    ).toBe('https://api.example.vn/api/v1')
  })

  it('resolves the share API on the page HTTPS origin, never the visitor localhost', () => {
    expect(resolveApiBaseUrl('/api/v1', 'https://demo.trycloudflare.com/chat'))
      .toBe('https://demo.trycloudflare.com/api/v1')
    expect(resolveApiBaseUrl('/api/v1/', 'http://localhost:3016/'))
      .toBe('http://localhost:3016/api/v1')
  })

  it('scopes requests to the selected tab account, without exposing session secrets', async () => {
    selectAccount('account-a')
    fetchMock.mockResolvedValue(new Response(JSON.stringify({ id: 'account-a' })))
    await getCurrentUser()
    const options = fetchMock.mock.calls[0][1] as RequestInit
    expect(new Headers(options.headers).get('X-Account-ID')).toBe('account-a')
    expect(new Headers(options.headers).has('Authorization')).toBe(false)
    expect(options.credentials).toBe('include')
    expect(sessionStorage.getItem('hcc-account-id')).toBe('account-a')
  })

  it('never bypasses a full queue through the legacy SSE endpoint', async () => {
    fetchMock.mockResolvedValue(new Response(JSON.stringify({ error: { code: 'QUEUE_FULL', message: 'Đang đông người.' } }), { status: 429 }))
    await expect(streamMessage('thread', { client_message_id: 'id', content: 'test' }, vi.fn())).rejects.toMatchObject({ code: 'QUEUE_FULL', status: 429 })
    expect(fetchMock).toHaveBeenCalledOnce()
    expect(fetchMock.mock.calls[0][0]).toContain('/jobs')
  })

  it('uses slower queue polling after a blocked socket and keeps the same job', async () => {
    vi.useFakeTimers()
    vi.stubGlobal('WebSocket', class { constructor() { throw new Error('blocked') } })
    const queued = { job_id: 'job-a', conversation_id: 'thread-a', state: 'queued' as const, poll_after_ms: 2000 }
    fetchMock.mockResolvedValueOnce(new Response(JSON.stringify(queued)))
    fetchMock.mockResolvedValueOnce(new Response(JSON.stringify({ ...queued, state: 'completed', result: { marker: 'done' } })))
    const result = watchJob(queued, vi.fn())
    await vi.advanceTimersByTimeAsync(1999)
    expect(fetchMock).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(1)
    expect(await result).toEqual({ marker: 'done' })
    expect(fetchMock.mock.calls.every(([url]) => String(url).endsWith('/jobs/job-a'))).toBe(true)
  })

  it('decodes split UTF-8 SSE events and requires a complete verified result', async () => {
    const encoder = new TextEncoder()
    const encoded = encoder.encode('event: delta\ndata: {"text":"Giấy tờ"}\n\nevent: complete\ndata: {"marker":"verified"}\n\n')
    fetchMock.mockResolvedValue(new Response(new ReadableStream({ start(controller) {
      for (const byte of encoded) controller.enqueue(new Uint8Array([byte]))
      controller.close()
    } })))
    const onEvent = vi.fn()
    expect(await streamMessageSSE('thread', { client_message_id: 'id', content: 'test' }, onEvent)).toEqual({ marker: 'verified' })
    expect(onEvent).toHaveBeenCalledWith({ type: 'delta', text: 'Giấy tờ' })
  })

  it('always sends cookies but does not force a JSON preflight header on GET', async () => {
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ id: 'user-1', display_name: 'Tú' }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    )

    await getCurrentUser()

    expect(fetchMock).toHaveBeenCalledOnce()
    const [url, options] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('http://localhost:8000/api/v1/auth/me')
    expect(options.credentials).toBe('include')
    expect(new Headers(options.headers).has('Content-Type')).toBe(false)
  })

  it('sends the login JSON using the frozen contract', async () => {
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ id: 'user-1', display_name: 'Tú' }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    )

    await login('Tú')

    const [url, options] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('http://localhost:8000/api/v1/auth/login')
    expect(options.method).toBe('POST')
    expect(options.body).toBe(JSON.stringify({ display_name: 'Tú' }))
    expect(new Headers(options.headers).get('Content-Type')).toBe('application/json')
  })

  it('preserves the backend error code and request id', async () => {
    fetchMock.mockResolvedValue(
      new Response(
        JSON.stringify({
          error: {
            code: 'AI_SERVICE_TIMEOUT',
            message: 'AI đang phản hồi chậm.',
            request_id: 'request-1',
          },
        }),
        { status: 504, headers: { 'Content-Type': 'application/json' } },
      ),
    )

    const request = login('Tú')
    await expect(request).rejects.toMatchObject({
      status: 504,
      code: 'AI_SERVICE_TIMEOUT',
      requestId: 'request-1',
    } satisfies Partial<ApiError>)
  })

  it('sends both feedback axes with PUT and credentials', async () => {
    fetchMock.mockResolvedValue(
      new Response(
        JSON.stringify({
          id: 'feedback-1',
          message_id: 'assistant-1',
          relevance: 'relevant',
          satisfaction: 'satisfied',
          created_at: '2026-09-13T01:00:00Z',
          updated_at: '2026-09-13T01:00:00Z',
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    )

    await putMessageFeedback('assistant-1', {
      relevance: 'relevant',
      satisfaction: 'satisfied',
      reason: null,
    })

    const [url, options] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('http://localhost:8000/api/v1/messages/assistant-1/feedback')
    expect(options.method).toBe('PUT')
    expect(options.credentials).toBe('include')
    expect(options.body).toBe(
      JSON.stringify({ relevance: 'relevant', satisfaction: 'satisfied', reason: null }),
    )
  })
})
