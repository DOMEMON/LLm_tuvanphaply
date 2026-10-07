import { useCallback, useEffect, useRef, useState } from 'react'
import * as Dropdown from '@radix-ui/react-dropdown-menu'
import { ArrowUp, ArrowUpRight, Buildings, CaretUpDown, Check, DotsThree, HouseLine, MagnifyingGlass, Moon, NotePencil, PencilSimple, PushPin, SealCheck, SidebarSimple, SignOut, Sun, Trash, UserPlus, X, ArrowCounterClockwise, LockKey, ChatsCircle, Monitor } from '@phosphor-icons/react'
import { AuthDialog } from './components/AuthDialog'
import { Modal } from './components/Modal'
import { MessageView } from './components/MessageView'
import { PreparationPanel } from './components/PreparationPanel'
import * as api from './services/api'
import { restoreBrowserSession, restoredConversation } from './services/browserSession'
import type { Conversation, Message, MessageFeedback, User } from './types/api'
import './App.css'
export { FeedbackPanel } from './components/MessageView'

type Theme = 'system' | 'light' | 'dark'
type Attempt = { conversationId: string; content: string; clientMessageId: string }
const SUGGESTIONS = [
  { icon: NotePencil, title: 'Đăng ký khai sinh', prompt: 'Tôi muốn đăng ký khai sinh cho con, cần chuẩn bị giấy tờ gì?' },
  { icon: HouseLine, title: 'Xây dựng nhà ở', prompt: 'Tôi chuẩn bị xây nhà, hồ sơ cấp giấy phép xây dựng cần những gì?' },
  { icon: Buildings, title: 'Hộ kinh doanh', prompt: 'Tôi muốn tạm ngừng hộ kinh doanh, hồ sơ cần gì?' },
]
const errorText = (error: unknown) => error instanceof Error ? error.message : 'Có lỗi xảy ra. Vui lòng thử lại.'
const activeKey = (id: string) => 'hcc-conversation:' + id
const visitorMode = import.meta.env.VITE_VISITOR_MODE === 'true'
const requireAccount = !visitorMode && import.meta.env.VITE_REQUIRE_ACCOUNT === 'true'
const historyStorage = visitorMode ? localStorage : sessionStorage
const order = (items: Conversation[]) => [...items].sort((a, b) => Number(Boolean(b.is_pinned)) - Number(Boolean(a.is_pinned)) || b.updated_at.localeCompare(a.updated_at))

function App() {
  const [user, setUser] = useState<User | null>(null)
  const [accounts, setAccounts] = useState<User[]>([])
  const [conversations, setConversations] = useState<Conversation[]>([])
  const [active, setActive] = useState<Conversation | null>(null)
  const [messages, setMessages] = useState<Message[]>([])
  const [feedback, setFeedback] = useState<Record<string, MessageFeedback>>({})
  const [draft, setDraft] = useState('')
  const [search, setSearch] = useState('')
  const [topicFilter, setTopicFilter] = useState('')
  const [loading, setLoading] = useState(true)
  const [loadingHistory, setLoadingHistory] = useState(false)
  const [sending, setSending] = useState(false)
  const [retry, setRetry] = useState<Attempt | null>(null)
  const [error, setError] = useState('')
  const [status, setStatus] = useState('')
  const [partial, setPartial] = useState('')
  const [authMode, setAuthMode] = useState<'login' | 'register' | 'password' | null>(null)
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [mobile, setMobile] = useState(() => window.matchMedia('(max-width: 760px)').matches)
  const [dialog, setDialog] = useState<{ action: 'rename' | 'delete'; conversation: Conversation } | null>(null)
  const [newTitle, setNewTitle] = useState('')
  const [preparation, setPreparation] = useState<{ messageId?: string; procedureId?: string } | null>(null)
  const [mutationBusy, setMutationBusy] = useState(false)
  const [theme, setTheme] = useState<Theme>(() => (localStorage.getItem('hcc-theme') as Theme) || 'system')
  const epoch = useRef(0)
  const activeRef = useRef<string | null>(null)
  const currentUserRef = useRef<User | null>(null)
  const sendLock = useRef(false)
  const composer = useRef<HTMLTextAreaElement>(null)
  const scrollArea = useRef<HTMLDivElement>(null)
  const scrollBottom = useRef<HTMLDivElement>(null)
  const nearBottom = useRef(true)
  const drawer = useRef<HTMLElement>(null)

  useEffect(() => {
    const media = window.matchMedia('(max-width: 760px)')
    const change = () => setMobile(media.matches)
    media.addEventListener('change', change)
    return () => media.removeEventListener('change', change)
  }, [])
  useEffect(() => {
    if (!mobile || !sidebarOpen) return
    const previous = document.activeElement as HTMLElement | null
    drawer.current?.querySelector<HTMLElement>('button')?.focus()
    return () => previous?.focus()
  }, [mobile, sidebarOpen])

  useEffect(() => {
    const media = window.matchMedia('(prefers-color-scheme: dark)')
    const apply = () => { document.documentElement.dataset.theme = theme === 'system' ? (media.matches ? 'dark' : 'light') : theme }
    apply(); localStorage.setItem('hcc-theme', theme)
    media.addEventListener('change', apply)
    return () => media.removeEventListener('change', apply)
  }, [theme])

  useEffect(() => {
    if (composer.current) { composer.current.style.height = 'auto'; composer.current.style.height = Math.min(composer.current.scrollHeight, 180) + 'px' }
  }, [draft, active])

  useEffect(() => {
    if (nearBottom.current) scrollBottom.current?.scrollIntoView?.({ block: 'end' })
  }, [messages, partial, status])

  function broadcastAccounts() {
    if ('BroadcastChannel' in window) { const channel = new BroadcastChannel('hcc-accounts'); channel.postMessage('changed'); channel.close() }
  }
  const resetWorkspace = useCallback(() => {
    setPreparation(null)
    setTopicFilter('')
    epoch.current++; activeRef.current = null
    setActive(null); setConversations([]); setMessages([]); setFeedback({}); setDraft(''); setRetry(null); setSearch(''); setError(''); setLoadingHistory(false); setSidebarOpen(false); setPartial(''); setStatus('')
  }, [])
  const reportError = useCallback((err: unknown) => {
    if (err instanceof api.ApiError && err.status === 401) {
      resetWorkspace(); api.selectAccount(null); currentUserRef.current = null; setUser(null)
    }
    setError(errorText(err))
  }, [resetWorkspace])
  const selectConversation = useCallback(async (item: Conversation) => {
    if (sendLock.current) return
    const version = ++epoch.current
    activeRef.current = item.id; setActive(item); setMessages([]); setFeedback({}); setDraft(''); setRetry(null); setError(''); setSidebarOpen(false); setLoadingHistory(true); nearBottom.current = true
    if (currentUserRef.current) historyStorage.setItem(activeKey(currentUserRef.current.id), item.id)
    try {
      const [history, saved, job] = await Promise.all([api.getMessages(item.id), api.getConversationFeedback(item.id), api.getActiveJob(item.id)])
      if (version !== epoch.current) return
      setMessages(history); setFeedback(Object.fromEntries(saved.map((entry) => [entry.message_id, entry])))
      if (job) {
        sendLock.current = true; setSending(true); setLoadingHistory(false)
        if (job.client_message_id && job.content) {
          setRetry({ conversationId: item.id, clientMessageId: job.client_message_id, content: job.content })
          if (!history.some((entry) => entry.role === 'user' && entry.status === 'pending')) {
            setMessages((current) => [...current, { id: 'pending:' + job.client_message_id, conversation_id: item.id, role: 'user', content: job.content!, status: 'pending', created_at: new Date().toISOString() }])
          }
        }
        try {
          const result = await api.watchJob(job, (event) => { if (version === epoch.current) setStatus(event.message ?? 'Đang xử lý yêu cầu đã lưu…') })
          if (version === epoch.current) {
            setRetry(null)
            setMessages((current) => [...current.filter((entry) => entry.id !== 'pending:' + job.client_message_id && entry.id !== result.user_message.id && entry.id !== result.assistant_message.id), result.user_message, result.assistant_message])
            const items = await api.getConversations()
            if (version === epoch.current) { setConversations(order(items)); setActive(items.find((entry) => entry.id === item.id) ?? null) }
          }
        } catch (err) {
          if (version === epoch.current) setMessages((current) => current.map((entry) => entry.status === 'pending' ? { ...entry, status: 'failed' } : entry))
          throw err
        } finally { sendLock.current = false; setSending(false); setStatus('') }
      }
    } catch (err) { if (version === epoch.current) reportError(err) }
    finally { if (version === epoch.current) setLoadingHistory(false) }
  }, [reportError])
  const activateAccount = useCallback(async (account: User, restore = false) => {
    resetWorkspace(); api.selectAccount(account.id); currentUserRef.current = account; setUser(account)
    const version = epoch.current
    try {
      const items = await api.getConversations()
      if (version !== epoch.current) return
      setConversations(order(items))
      const remembered = restore ? historyStorage.getItem(activeKey(account.id)) : null
      const selected = restoredConversation(order(items), remembered, visitorMode)
      if (selected) await selectConversation(selected)
    } catch (err) { if (version === epoch.current) reportError(err) }
  }, [resetWorkspace, reportError, selectConversation])
  useEffect(() => {
    let disposed = false
    const lifetime = epoch
    async function restore() {
      try {
        if (visitorMode) {
          const account = await restoreBrowserSession(api.getApiBaseUrl(), {
            accounts: api.getAccounts, guest: api.startGuest,
            renew: async (account) => { api.selectAccount(account.id); return api.renewBrowserSession() },
            remembered: () => sessionStorage.getItem('hcc-account-id'),
          })
          if (!disposed) { setAccounts([account]); await activateAccount(account, true) }
          return
        }
        const available = await api.getAccounts()
        if (disposed) return
        const selectable = requireAccount ? available.filter((item) => !item.is_guest) : available
        setAccounts(selectable)
        const remembered = sessionStorage.getItem('hcc-account-id')
        const chosen = selectable.find((item) => item.id === remembered) ?? (!remembered ? selectable[0] : null)
        if (chosen) await activateAccount(chosen, true)
        else api.selectAccount(null)
      } catch (err) { if (!disposed) setError(errorText(err)) }
      finally { if (!disposed) setLoading(false) }
    }
    void restore()
    return () => { disposed = true; lifetime.current++ }
  }, [activateAccount])

  useEffect(() => {
    if (!('BroadcastChannel' in window)) return
    const channel = new BroadcastChannel('hcc-accounts')
    channel.onmessage = async () => {
      const existing = currentUserRef.current
      try {
        const available = await api.getAccounts()
        const selectable = requireAccount ? available.filter((item) => !item.is_guest) : available
        setAccounts(selectable)
        if (existing && currentUserRef.current?.id === existing.id && !selectable.some((item) => item.id === existing.id)) {
          resetWorkspace(); setUser(null); currentUserRef.current = null; api.selectAccount(null)
          setError('Tài khoản này đã đăng xuất ở tab khác. Bạn có thể đăng nhập lại.')
        }
      } catch { /* Recheck on the next authenticated request. */ }
    }
    return () => channel.close()
  }, [resetWorkspace])

  async function authenticated(account: User) {
    setAuthMode(null)
    const savedDraft = draft
    const upgrading = currentUserRef.current?.id === account.id
    api.selectAccount(account.id); currentUserRef.current = account; setUser(account)
    if (!upgrading) { await activateAccount(account); setDraft(savedDraft) }
    else setAccounts((current) => current.map((item) => item.id === account.id ? account : item))
    try { setAccounts(await api.getAccounts()) } catch (err) { reportError(err) }
    broadcastAccounts()
  }
  function newConversation() {
    if (sendLock.current) return
    epoch.current++; activeRef.current = null; setActive(null); setMessages([]); setFeedback({}); setDraft(''); setRetry(null); setError(''); setSidebarOpen(false); setLoadingHistory(false)
    if (currentUserRef.current) {
      if (visitorMode) historyStorage.setItem(activeKey(currentUserRef.current.id), '')
      else historyStorage.removeItem(activeKey(currentUserRef.current.id))
    }
    composer.current?.focus()
  }
  async function send(existing?: Attempt) {
    const content = existing?.content ?? draft.trim()
    if (!content || sendLock.current || loadingHistory) return
    if (requireAccount && (!currentUserRef.current || currentUserRef.current.is_guest)) {
      setAuthMode('register')
      return
    }
    sendLock.current = true; setSending(true); setError(''); setDraft(''); setPartial(''); setStatus('Đang chuẩn bị câu trả lời…'); nearBottom.current = true
    let attempt = existing
    let optimisticId = ''
    const version = epoch.current
    try {
      let account = currentUserRef.current
      if (!account) {
        account = await api.startGuest(); api.selectAccount(account.id); currentUserRef.current = account; setUser(account); setAccounts((current) => [...current, account!]); broadcastAccounts()
      }
      let threadId = existing?.conversationId ?? activeRef.current
      if (!threadId) {
        const item = await api.createConversation(); threadId = item.id
        if (version !== epoch.current) return
        setActive(item); activeRef.current = item.id; setConversations((current) => order([item, ...current])); historyStorage.setItem(activeKey(account.id), item.id)
      }
      attempt = existing ?? { conversationId: threadId, content, clientMessageId: crypto.randomUUID() }
      setRetry(attempt)
      optimisticId = 'pending:' + attempt.clientMessageId
      setMessages((current) => [...current.filter((item) => item.id !== optimisticId), { id: optimisticId, conversation_id: threadId!, role: 'user', content, status: 'pending', created_at: new Date().toISOString() }])
      const result = await api.streamMessage(threadId, { client_message_id: attempt.clientMessageId, content }, (event) => {
        if (version !== epoch.current) return
        if (event.type === 'status') setStatus(event.message ?? 'Đang đối chiếu thông tin…')
        if (event.type === 'delta') setPartial((current) => current + (event.text ?? ''))
      })
      if (version !== epoch.current) return
      setMessages((current) => [...current.filter((item) => item.id !== optimisticId && item.id !== result.user_message.id && item.id !== result.assistant_message.id), result.user_message, result.assistant_message])
      setRetry(null); setPartial('')
      const items = await api.getConversations()
      if (version === epoch.current) { setConversations(order(items)); setActive(items.find((item) => item.id === threadId) ?? null) }
    } catch (err) {
      if (version === epoch.current) {
        setMessages((current) => current.map((item) => item.id === optimisticId ? { ...item, status: 'failed' } : item))
        if (!attempt) setDraft(content)
        reportError(err)
      }
    } finally { sendLock.current = false; setSending(false); setStatus(''); setPartial('') }
  }
  async function signOut() {
    if (sendLock.current) return
    try { await api.logout(); resetWorkspace(); currentUserRef.current = null; setUser(null); api.selectAccount(null); setAccounts(await api.getAccounts()); broadcastAccounts() }
    catch (err) { reportError(err) }
  }
  async function pin(item: Conversation) {
    try { const changed = await api.updateConversation(item.id, { is_pinned: !item.is_pinned }); setConversations((current) => order(current.map((entry) => entry.id === item.id ? changed : entry))); if (activeRef.current === item.id) setActive(changed) }
    catch (err) { reportError(err) }
  }
  async function confirmMutation() {
    if (!dialog || mutationBusy) return
    setMutationBusy(true)
    try {
      if (dialog.action === 'rename') {
        const changed = await api.updateConversation(dialog.conversation.id, { title: newTitle.trim() })
        setConversations((current) => order(current.map((item) => item.id === changed.id ? changed : item)))
        if (activeRef.current === changed.id) setActive(changed)
      } else {
        await api.deleteConversation(dialog.conversation.id)
        setConversations((current) => current.filter((item) => item.id !== dialog.conversation.id))
        if (activeRef.current === dialog.conversation.id) newConversation()
      }
      setDialog(null)
    } catch (err) { reportError(err) } finally { setMutationBusy(false) }
  }
  const topics = [...new Set(conversations.map((item) => item.topic).filter((topic): topic is string => Boolean(topic)))].sort()
  const visible = conversations.filter((item) => (!topicFilter || item.topic === topicFilter) && (item.title + ' ' + (item.topic ?? '')).toLocaleLowerCase('vi').includes(search.toLocaleLowerCase('vi')))
  const empty = !active && messages.length === 0
  const greeting = user && !user.is_guest ? 'Chào ' + user.display_name.split(' ').at(-1) + ',' : 'Chào bạn,'

  function threadRow(item: Conversation) {
    return <div className={'thread-row ' + (active?.id === item.id ? 'active' : '')} key={item.id}>
      <button className="thread-select" disabled={sending} onClick={() => void selectConversation(item)} title={item.title}>{item.is_pinned ? <PushPin size={15} /> : <ChatsCircle size={15} />}<span>{item.title}</span></button>
      <Dropdown.Root><Dropdown.Trigger asChild><button className="icon-button thread-more" aria-label={'Tùy chọn: ' + item.title} disabled={sending}><DotsThree size={21} weight="bold" /></button></Dropdown.Trigger><Dropdown.Portal><Dropdown.Content className="dropdown" side="right" align="start" sideOffset={6}>
        <Dropdown.Item className="dropdown-item" onSelect={() => void pin(item)}><PushPin size={16} />{item.is_pinned ? 'Bỏ ghim' : 'Ghim cuộc trò chuyện'}</Dropdown.Item>
        <Dropdown.Item className="dropdown-item" onSelect={() => { setNewTitle(item.title); setDialog({ action: 'rename', conversation: item }) }}><PencilSimple size={16} />Đổi tên</Dropdown.Item>
        <Dropdown.Separator className="dropdown-separator" /><Dropdown.Item className="dropdown-item danger" onSelect={() => setDialog({ action: 'delete', conversation: item })}><Trash size={16} />Xóa cuộc trò chuyện</Dropdown.Item>
      </Dropdown.Content></Dropdown.Portal></Dropdown.Root>
    </div>
  }

  const composerBox = <form className={'composer ' + (empty ? 'composer-home' : '')} onSubmit={(event) => { event.preventDefault(); void send() }}>
    <textarea ref={composer} aria-label="Câu hỏi của bạn" placeholder={empty ? 'Bạn cần hỗ trợ thủ tục gì?' : 'Hỏi thêm về thủ tục…'} value={draft} onChange={(event) => setDraft(event.target.value)} maxLength={4000} rows={2} disabled={loading || sending || loadingHistory} onKeyDown={(event) => { if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); void send() } }} />
    <div className="composer-toolbar"><span><SealCheck size={15} /> Trả lời có nguồn tham chiếu</span><button type="submit" className="send-button" aria-label="Gửi câu hỏi" title="Gửi câu hỏi" disabled={!draft.trim() || loading || sending || loadingHistory}><ArrowUp size={22} weight="bold" /></button></div>
  </form>

  return <div className={'app-shell ' + (sidebarOpen ? 'sidebar-is-open' : '')}>
    {sidebarOpen && <button className="sidebar-scrim" aria-label="Đóng danh sách hội thoại" onClick={() => setSidebarOpen(false)} />}
    <aside ref={drawer} className="sidebar" aria-label="Điều hướng" inert={mobile && !sidebarOpen} onKeyDown={(event) => {
      if (!mobile || !sidebarOpen) return
      if (event.key === 'Escape') { event.preventDefault(); setSidebarOpen(false) }
      if (event.key === 'Tab') {
        const elements = drawer.current?.querySelectorAll<HTMLElement>('button:not(:disabled), input, select, [tabindex="0"]')
        if (!elements?.length) return
        const first = elements[0], last = elements[elements.length - 1]
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus() }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus() }
      }
    }}>
      <div className="sidebar-brand"><button className="brand" onClick={newConversation} disabled={sending}><span className="brand-mark"><SealCheck size={24} weight="fill" /></span><span>Hành chính<span className="brand-caption">G8 · Đa yêu cầu</span></span></button><button className="icon-button mobile-close" aria-label="Đóng menu" onClick={() => setSidebarOpen(false)}><X size={20} /></button></div>
      <button className="new-chat" onClick={newConversation} disabled={sending}><NotePencil size={19} />Cuộc trò chuyện mới<span className="key-hint">+</span></button>
      <button className="new-chat" disabled={!user || loading} onClick={() => { setSidebarOpen(false); setPreparation({}) }}><SealCheck size={19} />Hồ sơ đang chuẩn bị</button>
      <label className="search-box"><MagnifyingGlass size={17} /><input aria-label="Tìm cuộc trò chuyện" placeholder="Tìm cuộc trò chuyện" value={search} onChange={(event) => setSearch(event.target.value)} /></label>
      {topics.length > 0 && <select className="topic-filter" aria-label="Lọc theo thủ tục" value={topicFilter} onChange={(event) => setTopicFilter(event.target.value)}><option value="">Tất cả thủ tục</option>{topics.map((topic) => <option key={topic} value={topic}>{topic}</option>)}</select>}
      <nav className="conversation-list" aria-label="Danh sách cuộc trò chuyện">
        {loading ? <div className="sidebar-loading"><span /><span /><span /></div> : visible.length ? <>
          {visible.some((item) => item.is_pinned) && <section><h2>Đã ghim</h2>{visible.filter((item) => item.is_pinned).map(threadRow)}</section>}
          {visible.some((item) => !item.is_pinned) && <section><h2>Gần đây</h2>{visible.filter((item) => !item.is_pinned).map(threadRow)}</section>}
        </> : <div className="history-empty"><ChatsCircle size={25} /><p>{search ? 'Không tìm thấy cuộc trò chuyện.' : 'Mọi cuộc trò chuyện của bạn sẽ ở đây.'}</p></div>}
      </nav>
      <div className="sidebar-bottom">
        {visitorMode && <div className="guest-note"><span>Lịch sử gắn với cookie của trình duyệt này. Xóa cookie hoặc dùng thiết bị khác sẽ không khôi phục được.</span></div>}
        {!visitorMode && user?.is_guest && <div className="guest-note"><span>Lưu lại những điều quan trọng.</span><button className="text-button" onClick={() => setAuthMode('register')} disabled={sending}>Tạo tài khoản <ArrowUpRight size={14} /></button></div>}
        <Dropdown.Root><Dropdown.Trigger asChild><button className="account-trigger" disabled={sending || loading}><span className="avatar">{user && !user.is_guest ? user.display_name.charAt(0).toUpperCase() : 'K'}</span><span className="account-info"><strong>{visitorMode ? 'Phiên trình duyệt' : user?.display_name ?? 'Chưa đăng nhập'}</strong><small>{visitorMode ? 'Tự lưu lịch sử · Không cần đăng nhập' : user?.username ? '@' + user.username : user?.email ?? 'Không gian cá nhân'}</small></span><CaretUpDown size={16} /></button></Dropdown.Trigger><Dropdown.Portal><Dropdown.Content className="dropdown account-dropdown" side="top" align="start" sideOffset={10}>
          <Dropdown.Label className="dropdown-label">{visitorMode ? "Lịch sử trên trình duyệt này" : "Tài khoản trong trình duyệt"}</Dropdown.Label>
          {!visitorMode && <>
          {accounts.map((account) => <Dropdown.Item key={account.id} className="dropdown-item account-option" onSelect={() => void activateAccount(account)}><span className="avatar small">{account.display_name.charAt(0)}</span><span>{account.display_name}<small>{account.username ? '@' + account.username : account.is_guest ? 'Phiên khách' : account.email}</small></span>{account.id === user?.id && <Check size={16} />}</Dropdown.Item>)}
          <Dropdown.Item className="dropdown-item" onSelect={() => setAuthMode('login')}><UserPlus size={17} />{user ? 'Thêm tài khoản' : 'Đăng nhập'}</Dropdown.Item>
          {user && !user.is_guest && <Dropdown.Item className="dropdown-item" onSelect={() => setAuthMode('password')}><LockKey size={17} />Thiết lập mật khẩu</Dropdown.Item>}
          </>}
          <Dropdown.Separator className="dropdown-separator" /><Dropdown.Label className="dropdown-label">Giao diện</Dropdown.Label>
          {([['light', 'Sáng', Sun], ['dark', 'Tối', Moon], ['system', 'Theo thiết bị', Monitor]] as const).map(([value, label, Icon]) => <Dropdown.Item key={value} className="dropdown-item" onSelect={() => setTheme(value)}><Icon size={17} />{label}{theme === value && <Check size={15} className="menu-check" />}</Dropdown.Item>)}
          {!visitorMode && user && <><Dropdown.Separator className="dropdown-separator" /><Dropdown.Item className="dropdown-item" onSelect={() => void signOut()}><SignOut size={17} />Đăng xuất tài khoản này</Dropdown.Item></>}
        </Dropdown.Content></Dropdown.Portal></Dropdown.Root>
      </div>
    </aside>
    <main className="workspace" inert={mobile && sidebarOpen}>
      <header className="chat-header"><div className="header-leading"><button className="icon-button sidebar-toggle" aria-label="Mở danh sách hội thoại" onClick={() => setSidebarOpen(true)}><SidebarSimple size={22} /></button><span className="header-title">{active?.title ?? 'Không gian hỏi đáp'}</span></div><div className="header-actions"><span className="local-badge"><SealCheck size={15} />Thông tin có căn cứ</span>{!visitorMode && (!user || user.is_guest) && <button className="secondary-button" onClick={() => setAuthMode('login')} disabled={sending}>Đăng nhập</button>}</div></header>
      {error && <div className="error-banner" role="alert"><span>{error}</span>{retry && !sending && <button className="text-button" onClick={() => void send(retry)}><ArrowCounterClockwise size={16} />Thử lại</button>}<button className="icon-button" aria-label="Đóng thông báo" onClick={() => setError('')}><X size={17} /></button></div>}
      {empty ? <div className="welcome-scroll"><section className="welcome"><div className="welcome-mark"><SealCheck size={36} weight="fill" /></div><p className="welcome-greeting">{greeting}</p><h1>Thủ tục nào bạn đang cần?</h1><p className="welcome-description">Từ giấy tờ cần chuẩn bị đến nơi nộp hồ sơ.<br />Cứ hỏi theo cách của bạn.</p>{composerBox}<div className="suggestions"><span>Bắt đầu với</span>{SUGGESTIONS.map(({ icon: Icon, title, prompt }) => <button key={title} onClick={() => { setDraft(prompt); composer.current?.focus() }} disabled={loading || sending}><Icon size={17} />{title}<ArrowUpRight size={14} /></button>)}</div><p className="welcome-footnote">Bạn có thể hỏi tiếp hoặc đổi thủ tục bất cứ lúc nào.</p></section><div className="welcome-bottom"><span>Hỏi đơn giản. Hiểu rõ thủ tục.</span><span>Dữ liệu nội bộ đã duyệt cho demo</span></div></div>
        : <><div className="message-scroll" ref={scrollArea} onScroll={() => { const el = scrollArea.current; if (el) nearBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 160 }}><div className="message-column">
          {loadingHistory ? <div className="history-skeleton" role="status" aria-label="Đang tải hội thoại"><span /><span /><span /></div> : messages.map((message) => <MessageView key={message.id} message={message} feedback={feedback[message.id]} onChecklist={(messageId, procedureId) => setPreparation({ messageId, procedureId })} onFeedback={(value) => setFeedback((current) => { const next = { ...current }; if (value) next[message.id] = value; else delete next[message.id]; return next })} onError={reportError} />)}
          {sending && <div className="thinking" role="status"><span className="thinking-mark"><SealCheck size={20} /></span><span>{partial || status}</span></div>}<div ref={scrollBottom} />
        </div></div><div className="composer-dock">{composerBox}<p className="composer-note">Thông tin hỗ trợ tham khảo. Bạn có thể yêu cầu kiểm tra trên Cổng DVC Quốc gia.</p></div></>}
    </main>
    {preparation && user && <PreparationPanel key={user.id} messageId={preparation.messageId} procedureId={preparation.procedureId} onClose={() => setPreparation(null)} />}
    {!visitorMode && authMode && <AuthDialog initialMode={authMode} user={user} onSuccess={(account) => void authenticated(account)} onClose={() => setAuthMode(null)} />}
    {dialog && <Modal title={dialog.action === 'rename' ? 'Đổi tên cuộc trò chuyện' : 'Xóa cuộc trò chuyện?'} description={dialog.action === 'delete' ? '“' + dialog.conversation.title + '” cùng các tin nhắn và phản hồi sẽ bị xóa. Thao tác này không thể hoàn tác.' : 'Đặt một tên ngắn để dễ tìm lại.'} onClose={() => { if (!mutationBusy) setDialog(null) }}><form onSubmit={(event) => { event.preventDefault(); void confirmMutation() }}>{dialog.action === 'rename' && <label className="field-label">Tên cuộc trò chuyện<input autoFocus value={newTitle} onChange={(event) => setNewTitle(event.target.value)} required maxLength={200} /></label>}<div className="modal-actions"><button type="button" className="secondary-button" onClick={() => setDialog(null)} disabled={mutationBusy}>Hủy</button><button type="submit" className={dialog.action === 'delete' ? 'danger-button' : 'primary-button'} disabled={mutationBusy || (dialog.action === 'rename' && !newTitle.trim())}>{mutationBusy ? 'Đang lưu…' : dialog.action === 'delete' ? 'Xóa cuộc trò chuyện' : 'Lưu tên'}</button></div></form></Modal>}
  </div>
}

export default App
