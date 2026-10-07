import { useState } from 'react'
import { ArrowRight, Eye, EyeSlash } from '@phosphor-icons/react'
import * as api from '../services/api'
import type { User } from '../types/api'
import { Modal } from './Modal'

export function AuthDialog({ initialMode, user, onSuccess, onClose }: {
  initialMode: 'login' | 'register' | 'password'; user: User | null
  onSuccess: (user: User) => void; onClose: () => void
}) {
  const [mode, setMode] = useState(initialMode)
  const [username, setUsername] = useState(initialMode === 'password' ? user?.username ?? '' : '')
  const [displayName, setDisplayName] = useState('')
  const [password, setPassword] = useState('')
  const [confirmation, setConfirmation] = useState('')
  const [currentPassword, setCurrentPassword] = useState('')
  const [visible, setVisible] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  async function submit(event: React.FormEvent) {
    event.preventDefault()
    if (busy) return
    if (mode !== 'login' && password !== confirmation) { setError('Mật khẩu xác nhận chưa khớp.'); return }
    setBusy(true); setError('')
    try {
      const normalized = username.trim().toLowerCase()
      const result = mode === 'register'
        ? await api.register({ username: normalized, display_name: displayName.trim(), password, password_confirmation: confirmation })
        : mode === 'password'
          ? await api.changePassword({ username: normalized, password, password_confirmation: confirmation, ...(currentPassword ? { current_password: currentPassword } : {}) })
          : await api.passwordLogin(normalized, password)
      onSuccess(result)
    } catch (err) { setError(err instanceof Error ? err.message : 'Vui lòng thử lại.') }
    finally { setBusy(false) }
  }
  const title = mode === 'login' ? 'Chào mừng bạn trở lại' : mode === 'register' ? 'Tạo tài khoản của bạn' : 'Thiết lập mật khẩu'
  return <Modal title={title} description={mode === 'password' ? 'Dùng tên đăng nhập và mật khẩu để truy cập tài khoản này.' : 'Lưu cuộc trò chuyện và tiếp tục bất cứ khi nào bạn cần.'} onClose={() => { if (!busy) onClose() }}>
    <form className="auth-form" onSubmit={(event) => void submit(event)}>
      {mode === 'register' && <label>Họ và tên<input value={displayName} onChange={(event) => setDisplayName(event.target.value)} autoComplete="name" maxLength={100} required placeholder="Tên bạn muốn hiển thị" disabled={busy} /></label>}
      <label>Tên đăng nhập<input value={username} onChange={(event) => setUsername(event.target.value)} autoComplete="username" autoCapitalize="none" spellCheck={false} pattern="[a-zA-Z0-9_.\-]+" minLength={3} maxLength={50} required placeholder="Ví dụ: hoan.nguyen" disabled={busy} /></label>
      {mode === 'password' && user?.username && <label>Mật khẩu hiện tại<input type="password" value={currentPassword} onChange={(event) => setCurrentPassword(event.target.value)} autoComplete="current-password" required maxLength={128} disabled={busy} /></label>}
      <label>{mode === 'password' ? 'Mật khẩu mới' : 'Mật khẩu'}<span className="password-field"><input type={visible ? 'text' : 'password'} value={password} onChange={(event) => setPassword(event.target.value)} autoComplete={mode === 'login' ? 'current-password' : 'new-password'} minLength={mode === 'login' ? 1 : 8} maxLength={128} required placeholder={mode === 'login' ? 'Nhập mật khẩu của bạn' : 'Tối thiểu 8 ký tự'} disabled={busy} /><button className="icon-button" type="button" aria-label={visible ? 'Ẩn mật khẩu' : 'Hiện mật khẩu'} onClick={() => setVisible(!visible)}>{visible ? <EyeSlash size={19} /> : <Eye size={19} />}</button></span></label>
      {mode !== 'login' && <label>Xác nhận mật khẩu<input type={visible ? 'text' : 'password'} value={confirmation} onChange={(event) => setConfirmation(event.target.value)} autoComplete="new-password" minLength={8} maxLength={128} required disabled={busy} placeholder="Nhập lại mật khẩu" /></label>}
      {error && <p className="inline-error" role="alert">{error}</p>}
      <button className="primary-button auth-submit" disabled={busy} type="submit">{busy ? 'Vui lòng chờ…' : mode === 'login' ? 'Đăng nhập' : mode === 'register' ? 'Tạo tài khoản' : 'Lưu mật khẩu'}<ArrowRight size={18} /></button>
    </form>
    {mode !== 'password' && <p className="auth-switch">{mode === 'login' ? 'Bạn chưa có tài khoản?' : 'Bạn đã có tài khoản?'} <button className="text-button" disabled={busy} onClick={() => { setMode(mode === 'login' ? 'register' : 'login'); setError('') }}>{mode === 'login' ? 'Đăng ký' : 'Đăng nhập'}</button></p>}
    {user?.is_guest && mode === 'register' && <p className="form-note">Cuộc trò chuyện trong phiên khách này sẽ được giữ lại khi bạn tạo tài khoản.</p>}
  </Modal>
}
