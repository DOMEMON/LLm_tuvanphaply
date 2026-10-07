import type { User } from '../types/api'

type SessionOperations = {
  accounts: () => Promise<User[]>
  guest: () => Promise<User>
  remembered: () => string | null
  renew?: (account: User) => Promise<User>
}

// No token or conversation content is stored here. The server authenticates
// the opaque HttpOnly cookie; a public account ID is only a selection hint.
const pending = new Map<string, Promise<User>>()

export function restoreBrowserSession(scope: string, operations: SessionOperations): Promise<User> {
  const existing = pending.get(scope)
  if (existing) return existing

  async function restore() {
    const available = await operations.accounts()
    const remembered = operations.remembered()
    const account = available.find(account => account.id === remembered) ?? available[0]
    if (!account) return operations.guest()
    return operations.renew ? operations.renew(account) : account
  }

  // Serialize first visits across tabs, and recheck server cookies INSIDE the
  // lock. This also prevents React StrictMode from creating two guest users.
  const task = typeof navigator !== 'undefined' && navigator.locks
    ? navigator.locks.request('browser-session:' + scope, restore)
    : restore()
  const result = task.finally(() => pending.delete(scope))
  pending.set(scope, result)
  return result
}

export function restoredConversation<T extends { id: string }>(
  items: T[], remembered: string | null, visitorMode: boolean,
): T | undefined {
  // An empty string means the user explicitly chose a new, blank chat.
  return items.find(item => item.id === remembered)
    ?? (visitorMode && remembered === null ? items[0] : undefined)
}
