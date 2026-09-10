import type { ReactNode } from "react"

/**
 * The frame both plugin lists share: one bordered block, rows divided by the
 * row tier, so the installed set and the marketplace read as the same kind of
 * list rather than two pages that happen to sit on one.
 */
export function PluginList({
  ariaLabel,
  children,
}: {
  ariaLabel: string
  children: ReactNode
}) {
  return (
    <ul
      aria-label={ariaLabel}
      className="flex flex-col divide-y divide-border-subtle border border-border"
    >
      {children}
    </ul>
  )
}

/**
 * One plugin: its name and badges, what it does, a line of facts, and the
 * controls that act on it. Stacks below `md`, where the actions drop under the
 * text rather than squeezing it.
 */
export function PluginRow({
  title,
  badges,
  description,
  meta,
  error,
  actions,
}: {
  title: ReactNode
  badges?: ReactNode
  description: string
  /** Version, source, stars, links: the facts under the description. */
  meta?: ReactNode
  /** Why a plugin did not load, in the gateway's own words. */
  error?: string | null
  actions?: ReactNode
}) {
  return (
    <li className="flex flex-col gap-3 px-4 py-3 md:flex-row md:items-start md:gap-6">
      <div className="flex min-w-0 flex-1 flex-col gap-1">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-emphasis">{title}</span>
          {badges}
        </div>
        {description ? (
          <p className="max-w-prose text-caption text-subtle">{description}</p>
        ) : null}
        {meta ? (
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-caption">
            {meta}
          </div>
        ) : null}
        {error ? (
          <p className="max-w-prose break-words text-caption text-danger">
            {error}
          </p>
        ) : null}
      </div>
      {actions ? (
        <div className="flex shrink-0 flex-wrap items-center gap-2">
          {actions}
        </div>
      ) : null}
    </li>
  )
}
