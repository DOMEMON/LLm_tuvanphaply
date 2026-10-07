import * as Dialog from '@radix-ui/react-dialog'
import { X } from '@phosphor-icons/react'
import type { ReactNode } from 'react'

export function Modal({ title, description, children, onClose, wide = false }: {
  title: string; description?: string; children: ReactNode; onClose: () => void; wide?: boolean
}) {
  return <Dialog.Root open onOpenChange={(open) => { if (!open) onClose() }}>
    <Dialog.Portal>
      <Dialog.Overlay className="modal-overlay" />
      <Dialog.Content className={`modal ${wide ? 'modal-wide' : ''}`} aria-describedby={description ? 'modal-description' : undefined}>
        <Dialog.Close className="icon-button modal-close" aria-label="Đóng"><X size={20} /></Dialog.Close>
        <Dialog.Title className="modal-title">{title}</Dialog.Title>
        {description && <Dialog.Description id="modal-description" className="modal-description">{description}</Dialog.Description>}
        {children}
      </Dialog.Content>
    </Dialog.Portal>
  </Dialog.Root>
}
