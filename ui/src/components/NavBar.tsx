import { KeyboardEvent, ReactNode, useCallback, useEffect, useRef, useState } from 'react'
import { Link, useLocation, useNavigate } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'
import type { Role } from '../api/client'

// The pages grouped into menus (#616). Each role sees the pages it had before;
// a menu with none of them isn't shown. Hiding a page is a convenience: the
// router and the API refuse what the role can't do (#25).
interface PageEntry { label: string; to: string; roles: Role[] }
interface MenuDef { key: string; label: string; entries: PageEntry[] }

const STAFF: Role[] = ['engineer', 'developer']
const ENGINEER: Role[] = ['engineer']
const MENUS: MenuDef[] = [
  { key: 'data', label: 'Data', entries: [
    { label: 'Import', to: '/import', roles: STAFF },
    { label: 'Transfer', to: '/transfer', roles: STAFF },
  ] },
  { key: 'tune', label: 'Tune', entries: [
    { label: 'Chunking', to: '/chunking', roles: STAFF },
    { label: 'Retrieval', to: '/retrieval', roles: STAFF },
    { label: 'Gold Standard', to: '/goldstandard', roles: STAFF },
  ] },
  { key: 'admin', label: 'Admin', entries: [
    { label: 'Collections', to: '/collections', roles: ENGINEER },
    { label: 'Users', to: '/users', roles: ENGINEER },
    { label: 'Service keys', to: '/service-keys', roles: ENGINEER },
    { label: 'Audit', to: '/audit', roles: ENGINEER },
    { label: 'Health', to: '/health', roles: ENGINEER },
  ] },
]

type Item = { label: string; to: string; current: boolean } | { label: string; onSelect: () => void }
type Focus = 'first' | 'last'

const TOP = 'text-sm px-2 py-1 rounded hover:text-blue-600 focus-visible:outline focus-visible:outline-2 focus-visible:outline-blue-600'
const CURRENT = 'text-blue-700 font-semibold bg-blue-50'
const ENTRY = 'block w-full text-left whitespace-nowrap px-3 py-1.5 text-sm hover:bg-gray-100 focus:bg-gray-100 focus:outline-none'

// One menu button and its menu, after the WAI-ARIA menu button pattern: Enter,
// Space or ArrowDown opens it on the first entry, ArrowUp on the last; the
// arrows, Home and End move; Escape closes it and returns focus to the button;
// Tab closes it. Choosing an entry or a click outside closes it too.
function NavMenu({ menuKey, label, items, current, open, focus, align, onOpen, onClose }: {
  menuKey: string
  label: ReactNode
  items: Item[]
  current: boolean
  open: boolean
  focus: Focus
  align: 'left' | 'right'
  onOpen: (focus: Focus) => void
  onClose: () => void
}) {
  const wrapper = useRef<HTMLDivElement>(null)
  const button = useRef<HTMLButtonElement>(null)
  const menu = useRef<HTMLDivElement>(null)
  const menuId = `nav-menu-${menuKey}`
  const buttonId = `${menuId}-button`

  const entries = () => [...(menu.current?.querySelectorAll<HTMLElement>('[role=menuitem]') ?? [])]

  useEffect(() => {
    if (!open) return
    const all = entries()
    all[focus === 'last' ? all.length - 1 : 0]?.focus()
  }, [open, focus])

  useEffect(() => {
    if (!open) return
    const outside = (e: MouseEvent) => {
      if (wrapper.current && !wrapper.current.contains(e.target as Node)) onClose()
    }
    document.addEventListener('mousedown', outside)
    return () => document.removeEventListener('mousedown', outside)
  }, [open, onClose])

  function onButtonKey(e: KeyboardEvent<HTMLButtonElement>) {
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault()
      onOpen(e.key === 'ArrowUp' ? 'last' : 'first')
    }
  }

  function onMenuKey(e: KeyboardEvent<HTMLDivElement>) {
    const all = entries()
    const at = all.indexOf(document.activeElement as HTMLElement)
    const move = (i: number) => { e.preventDefault(); all[(i + all.length) % all.length]?.focus() }
    if (e.key === 'ArrowDown') move(at + 1)
    else if (e.key === 'ArrowUp') move(at - 1)
    else if (e.key === 'Home') move(0)
    else if (e.key === 'End') move(all.length - 1)
    else if (e.key === 'Escape') { e.preventDefault(); onClose(); button.current?.focus() }
    else if (e.key === 'Tab') onClose()
  }

  return (
    <div ref={wrapper} className={`relative${align === 'right' ? ' ml-auto' : ''}`}>
      <button
        ref={button}
        type="button"
        id={buttonId}
        data-menu={menuKey}
        data-current={current ? 'true' : undefined}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? menuId : undefined}
        onClick={() => (open ? onClose() : onOpen('first'))}
        onKeyDown={onButtonKey}
        className={`${TOP} flex items-center gap-1 ${current ? CURRENT : 'text-gray-700'}`}
      >
        {label}
        <span aria-hidden="true" className="text-xs">▾</span>
      </button>
      {open && (
        <div
          ref={menu}
          role="menu"
          id={menuId}
          aria-labelledby={buttonId}
          onKeyDown={onMenuKey}
          className={`absolute top-full mt-1 z-30 min-w-[11rem] bg-white border border-gray-200 rounded shadow-lg py-1 ${align === 'right' ? 'right-0' : 'left-0'}`}
        >
          {items.map(item => 'to' in item ? (
            <Link
              key={item.label}
              to={item.to}
              role="menuitem"
              tabIndex={-1}
              aria-current={item.current ? 'page' : undefined}
              onClick={onClose}
              className={`${ENTRY} ${item.current ? CURRENT : 'text-gray-700'}`}
            >
              {item.label}
            </Link>
          ) : (
            <button
              key={item.label}
              type="button"
              role="menuitem"
              tabIndex={-1}
              onClick={() => { onClose(); item.onSelect() }}
              className={`${ENTRY} text-gray-700`}
            >
              {item.label}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

export default function NavBar() {
  const { user, logout } = useAuth()
  const navigate = useNavigate()
  const { pathname } = useLocation()
  const role = user?.role ?? null
  const [open, setOpen] = useState<{ key: string; focus: Focus } | null>(null)

  // A change of page closes any open menu.
  useEffect(() => { setOpen(null) }, [pathname])

  async function logOut() {
    await logout()
    navigate('/login', { replace: true })
  }

  const roleLabel = role === 'engineer' ? 'AI Engineer' : role === 'developer' ? 'Developer' : 'End User'
  const close = useCallback(() => setOpen(null), [])
  const menuProps = (key: string) => ({
    menuKey: key,
    open: open?.key === key,
    focus: (open && open.key === key ? open.focus : 'first') as Focus,
    onOpen: (focus: Focus) => setOpen({ key, focus }),
    onClose: close,
  })

  const menus = MENUS
    .map(m => ({ ...m, entries: m.entries.filter(e => role !== null && e.roles.includes(role)) }))
    .filter(m => m.entries.length > 0)
  const accountCurrent = pathname === '/account/password'

  return (
    <nav className="bg-white border-b border-gray-200 px-4 py-3 flex items-center gap-4">
      <span className="font-bold text-blue-700 text-lg mr-2">RAG Platform</span>
      <Link
        to="/qa"
        aria-current={pathname === '/qa' ? 'page' : undefined}
        className={`${TOP} ${pathname === '/qa' ? CURRENT : 'text-gray-700'}`}
      >
        Q&amp;A
      </Link>
      {menus.map(m => (
        <NavMenu
          key={m.key}
          {...menuProps(m.key)}
          label={m.label}
          align="left"
          current={m.entries.some(e => e.to === pathname)}
          items={m.entries.map(e => ({ label: e.label, to: e.to, current: e.to === pathname }))}
        />
      ))}
      <NavMenu
        {...menuProps('account')}
        label={<>
          <span data-testid="nav-username">{user?.username}</span>{' '}
          <span className="text-xs bg-blue-100 text-blue-800 px-2 py-1 rounded">{roleLabel}</span>
        </>}
        align="right"
        current={accountCurrent}
        items={[
          { label: 'Change password', to: '/account/password', current: accountCurrent },
          { label: 'Log out', onSelect: logOut },
        ]}
      />
    </nav>
  )
}
