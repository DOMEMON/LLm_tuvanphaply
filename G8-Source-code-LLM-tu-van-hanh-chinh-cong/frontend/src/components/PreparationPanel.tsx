import { useEffect, useState } from 'react'
import { ArrowLeft, ArrowsClockwise, ClipboardText, Trash } from '@phosphor-icons/react'
import * as api from '../services/api'
import type { PreparationChecklist, PreparationStatus } from '../types/api'
import { Modal } from './Modal'

const LABELS: Record<PreparationStatus, string> = { unknown: 'Chưa rõ', have: 'Đã có', missing: 'Còn thiếu', not_applicable: 'Không áp dụng' }

export function PreparationPanel({ messageId, procedureId, onClose }: { messageId?: string; procedureId?: string; onClose: () => void }) {
  const [lists, setLists] = useState<PreparationChecklist[]>([])
  const [selected, setSelected] = useState<string | null>(null)
  const [busy, setBusy] = useState(true)
  const [error, setError] = useState('')
  const [confirm, setConfirm] = useState<'reset' | 'delete' | null>(null)
  const current = lists.find((l) => l.id === selected)
  useEffect(() => {
    let disposed = false
    async function load() {
      try {
        const created = messageId ? await (procedureId ? api.createChecklist(messageId, procedureId) : api.createChecklist(messageId)) : null
        const all = await api.getChecklists()
        if (!disposed) { setLists(all); setSelected(created?.id ?? null) }
      } catch (err) { if (!disposed) setError(err instanceof Error ? err.message : 'Chưa tải được checklist.') }
      finally { if (!disposed) setBusy(false) }
    }
    void load()
    return () => { disposed = true }
  }, [messageId, procedureId])
  async function mutate(action: () => Promise<PreparationChecklist | void>) {
    if (busy) return
    setBusy(true); setError('')
    try {
      const changed = await action()
      if (changed) setLists((all) => all.map((l) => l.id === changed.id ? changed : l))
      else { setLists((all) => all.filter((l) => l.id !== selected)); setSelected(null) }
      setConfirm(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Chưa lưu được thay đổi.')
      if (err instanceof api.ApiError && err.code === 'CHECKLIST_CONFLICT') {
        try { setLists(await api.getChecklists()) } catch { /* keep the error visible */ }
      }
    } finally { setBusy(false) }
  }
  return <Modal wide title="Hồ sơ đang chuẩn bị" description="Tiến độ của bạn được lưu riêng với cuộc trò chuyện. Chọn trạng thái theo giấy tờ bạn đang có." onClose={onClose}>
    <div className="preparation-panel" aria-busy={busy}>
      {error && <p role="alert" className="error-banner">{error}</p>}
      {busy && !lists.length && <p role="status">Đang tải hồ sơ…</p>}
      {current ? <>
        <button className="text-button" onClick={() => { setSelected(null); setConfirm(null) }} disabled={busy}><ArrowLeft size={16} /> Tất cả hồ sơ</button>
        <h3>{current.title}</h3>
        {current.source_changed && <p role="status">Nguồn đã có phiên bản mới. Hãy hỏi lại thành phần hồ sơ; checklist này giữ bản cũ để bạn đối chiếu.</p>}
        <p className="preparation-progress">Đã có {current.counts.have} / {current.items.length - current.counts.not_applicable} mục áp dụng theo lựa chọn của bạn</p>
        <p>{current.guidance}</p>
        <p className="source-note">Các nhánh hồ sơ được ghi riêng. Với mục không thuộc trường hợp của bạn, chọn “Không áp dụng”. Giữ nguyên điều kiện “nếu có”, “hoặc”, “một trong” ghi trong nguồn.</p>
        <ol className="preparation-items">{current.items.map((item, index) => <li key={item.id} className={`preparation-item status-${item.status}`}>
          {item.section && <p className="preparation-section">{item.section}</p>}
          <p className="preparation-text">{item.text}</p>
          <label>Trạng thái mục {index + 1}<select aria-label={`Trạng thái mục ${index + 1}`} value={item.status} disabled={busy} onChange={(event) => void mutate(() => api.changeChecklistItem(current.id, item.id, current.revision, event.target.value as PreparationStatus))}>
            {Object.entries(LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select></label>
          {item.status_source === 'USER_REPORTED' && <small>Bạn tự xác nhận</small>}
          {item.status === 'missing' && !item.forms?.length && <p className="source-note">Chưa có mẫu tải đã đối chiếu cho mục này.</p>}
          {item.status === 'missing' && !!item.forms?.length && <div className="preparation-forms" aria-label={`Mẫu tải cho mục ${index + 1}`}>
            {item.forms.map((form) => <div className="preparation-form" key={form.id}>
              <strong>{form.title}</strong>
              <small>{form.form_number} · {form.legal_basis}</small>
              <p>{form.applicability}</p>
              <p className="source-note">{form.extraction_note}</p>
              <div className="preparation-actions">
                <a href={api.getFormDownloadUrl(form.id)} target="_blank" rel="noopener noreferrer">Tải mẫu PDF</a>
                <a href={form.source_url} target="_blank" rel="noopener noreferrer">Đối chiếu nguồn gốc</a>
              </div>
              <small>{form.issuing_authority} · Đối chiếu {new Date(form.checked_at).toLocaleDateString('vi-VN')}</small>
            </div>)}
            <p className="source-note">Tải mẫu không có nghĩa đã chuẩn bị đủ giấy tờ. Điền và xin xác nhận theo yêu cầu ghi trên mẫu.</p>
          </div>}
        </li>)}</ol>
        <details className="technical-details"><summary>Nguồn của checklist</summary><p>Phiên bản: {current.corpus_version}. Tạo từ câu trả lời có nguồn đã duyệt; liên kết tải mẫu sẽ chỉ xuất hiện khi có mẫu được đối chiếu.</p></details>
        <div className="preparation-actions"><button className="text-button" disabled={busy} onClick={() => setConfirm('reset')}><ArrowsClockwise size={16} />Đặt lại trạng thái</button><button className="text-button" disabled={busy} onClick={() => setConfirm('delete')}><Trash size={16} />Xóa checklist</button></div>
        {confirm && <div className="preparation-confirm" role="group" aria-label="Xác nhận thay đổi checklist"><p>{confirm === 'delete' ? 'Xóa checklist và tiến độ đã lưu? Hội thoại vẫn được giữ.' : 'Đưa tất cả các mục về “Chưa rõ”?'}</p><button className="primary-button" disabled={busy} onClick={() => void mutate(() => confirm === 'delete' ? api.deleteChecklist(current.id, current.revision) : api.resetChecklist(current.id, current.revision))}>Xác nhận</button><button className="text-button" disabled={busy} onClick={() => setConfirm(null)}>Hủy</button></div>}
      </> : <>
        {!busy && !lists.length && <p>Chưa có checklist. Hãy hỏi hồ sơ của một thủ tục, rồi chọn “Lưu checklist hồ sơ” bên dưới câu trả lời.</p>}
        <div className="preparation-list">{lists.map((list) => <button key={list.id} disabled={busy} className="preparation-card" onClick={() => setSelected(list.id)}><ClipboardText size={21} /><span><strong>{list.title}</strong><small>{list.counts.have} mục đã có · {list.counts.missing} còn thiếu · {list.counts.unknown} chưa rõ</small></span></button>)}</div>
      </>}
    </div>
  </Modal>
}
