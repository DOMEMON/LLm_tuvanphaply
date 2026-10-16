import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, it, vi } from 'vitest'
import { MessageBoundary } from './MessageBoundary'

it('keeps normal message content intact', () => {
  render(<MessageBoundary><p>Nội dung hội thoại</p></MessageBoundary>)
  expect(screen.getByText('Nội dung hội thoại')).toBeInTheDocument()
  expect(screen.queryByRole('alert')).not.toBeInTheDocument()
})
it('offers recovery from a failed message chunk without blanking the workspace', async () => {
  vi.spyOn(console,'error').mockImplementation(()=>{})
  function FailedChunk(): never { throw new Error('Chunk download interrupted') }
  const reload = vi.fn()
  render(<MessageBoundary onReload={reload}><FailedChunk /></MessageBoundary>)
  expect(screen.getByRole('alert')).toHaveTextContent('Lịch sử đã lưu vẫn được giữ')
  await userEvent.click(screen.getByRole('button',{name:'Tải lại trang'}))
  expect(reload).toHaveBeenCalledOnce()
})
