import { Component } from 'react'
import type { ReactNode } from 'react'

// An outdated tab or interrupted chunk download must not blank the workspace.
export class MessageBoundary extends Component<{children: ReactNode; onReload?: () => void}, {failed: boolean}> {
  state = {failed:false}
  static getDerivedStateFromError() { return {failed:true} }
  render() {
    if (!this.state.failed) return this.props.children
    return <div className="error-banner" role="alert">
      <span>Không tải được phần hiển thị hội thoại. Lịch sử đã lưu vẫn được giữ.</span>
      <button className="text-button" onClick={this.props.onReload ?? (() => window.location.reload())}>Tải lại trang</button>
    </div>
  }
}
