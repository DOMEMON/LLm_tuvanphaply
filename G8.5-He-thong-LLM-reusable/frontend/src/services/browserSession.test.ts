import { afterEach, describe, expect, it, vi } from 'vitest'
import { restoreBrowserSession, restoredConversation } from './browserSession'
import type { User } from '../types/api'

const guest: User = { id: 'guest-one', display_name: 'Khách', is_guest: true }

afterEach(() => vi.unstubAllGlobals())

describe('cookie-backed browser identity', () => {
  it('creates a guest only when the server finds no valid cookie', async () => {
    const create = vi.fn().mockResolvedValue(guest)
    expect(await restoreBrowserSession('new', {
      accounts: async () => [], guest: create, remembered: () => null,
    })).toEqual(guest)
    expect(create).toHaveBeenCalledOnce()
  })

  it('restores cookies even after tab storage is cleared or stale', async () => {
    const create = vi.fn()
    expect(await restoreBrowserSession('old', {
      accounts: async () => [guest], guest: create, remembered: () => 'expired-id',
    })).toEqual(guest)
    expect(create).not.toHaveBeenCalled()
  })

  it('preserves a still-authenticated existing account', async () => {
    const account = { id: 'registered', display_name: 'Existing', is_guest: false }
    expect(await restoreBrowserSession('existing', {
      accounts: async () => [guest, account], guest: vi.fn(), remembered: () => account.id,
    })).toEqual(account)
  })

  it('renews an authenticated cookie without creating a new identity', async () => {
    const renew = vi.fn().mockResolvedValue(guest)
    const create = vi.fn()
    expect(await restoreBrowserSession('renew', {
      accounts: async () => [guest], guest: create, remembered: () => null, renew,
    })).toEqual(guest)
    expect(renew).toHaveBeenCalledWith(guest)
    expect(create).not.toHaveBeenCalled()
  })

  it('deduplicates simultaneous mounts and retries after failure', async () => {
    let release!: (value: User[]) => void
    const accounts = vi.fn(() => new Promise<User[]>(resolve => { release = resolve }))
    const create = vi.fn().mockResolvedValue(guest)
    const operations = { accounts, guest: create, remembered: () => null }
    const one = restoreBrowserSession('parallel', operations)
    const two = restoreBrowserSession('parallel', operations)
    expect(one).toBe(two)
    release([])
    await Promise.all([one, two])
    expect(create).toHaveBeenCalledOnce()
    await expect(restoreBrowserSession('retry', {
      ...operations, accounts: async () => { throw new Error('offline') },
    })).rejects.toThrow('offline')
    expect(await restoreBrowserSession('retry', {
      ...operations, accounts: async () => [guest],
    })).toEqual(guest)
  })

  it('rechecks cookies under a cross-tab lock before creating a user', async () => {
    const request = vi.fn(async (_name, callback) => callback())
    vi.stubGlobal('navigator', { locks: { request } })
    const accounts = vi.fn().mockResolvedValue([guest])
    const create = vi.fn()
    await restoreBrowserSession('locked', { accounts, guest: create, remembered: () => null })
    expect(request).toHaveBeenCalledWith('browser-session:locked', expect.any(Function))
    expect(accounts).toHaveBeenCalledOnce()
    expect(create).not.toHaveBeenCalled()
  })
})

describe('reopened conversation', () => {
  const items = [{ id: 'latest' }, { id: 'older' }]
  it('restores the explicitly selected thread', () => {
    expect(restoredConversation(items, 'older', true)?.id).toBe('older')
  })
  it('can restore from cookies alone, without local storage', () => {
    expect(restoredConversation(items, null, true)?.id).toBe('latest')
  })
  it('keeps a deliberately blank chat blank and leaves account mode unchanged', () => {
    expect(restoredConversation(items, '', true)).toBeUndefined()
    expect(restoredConversation(items, null, false)).toBeUndefined()
  })
})
