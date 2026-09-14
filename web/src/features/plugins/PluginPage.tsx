import { Link as ExternalLink } from "@heroui/react"
import { useNavigate, useParams } from "@tanstack/react-router"

import { EmptyState } from "@/design-system/feedback/EmptyState"
import { ErrorBanner } from "@/design-system/feedback/ErrorBanner"
import { PageLoading } from "@/design-system/feedback/PageLoading"
import { PageIntro } from "@/design-system/layout/PageIntro"
import { usePlugins } from "@/shared/api/plugins"

/**
 * A plugin's own page, framed.
 *
 * The gateway serves the plugin's static files at `ui.url`, on this origin, so
 * the frame authenticates with the same session cookie the dashboard holds and
 * needs nothing passed in. The `sandbox` attribute keeps scripts, forms, and
 * same-origin access, which is what a page that calls the plugin's own API
 * needs; with same-origin access granted it is not a trust boundary, and it
 * is not meant as one: the plugin already runs inside the gateway. It only
 * keeps a page from navigating the top window or locking the pointer by
 * accident.
 *
 * The frame is what scrolls: the page fills the content area and hands the
 * height to the frame, so an operator scrolls the plugin, not the dashboard
 * around it.
 */
export function PluginPage() {
  // Loose, because this component is mounted at the route rather than
  // importing it (a route file exports `Route` and nothing else), and a strict
  // read would need the route's own api.
  const { name } = useParams({ strict: false })
  const navigate = useNavigate()
  const plugins = usePlugins()

  if (plugins.isPending && !plugins.data) {
    return <PageLoading label="Reading installed plugins…" />
  }
  if (plugins.isError && !plugins.data) {
    return <ErrorBanner error={plugins.error} />
  }

  const plugin = plugins.data?.plugins.find((one) => one.name === name)
  const openMarketplace = () => void navigate({ to: "/marketplace" })

  if (!plugin) {
    return (
      <EmptyState
        title={`No plugin named ${name ?? ""}`}
        description="It is not installed on this gateway, or it was removed. The Marketplace lists what is installed."
        actionLabel="Open Marketplace"
        onAction={openMarketplace}
      />
    )
  }
  if (plugin.status === "failed") {
    return (
      <EmptyState
        title={`${plugin.name} did not load`}
        description="The gateway skipped this plugin at startup. The error it reported is on the Marketplace page, under Installed."
        actionLabel="Open Marketplace"
        onAction={openMarketplace}
      >
        {plugin.error ? (
          <p className="max-w-prose break-words text-caption text-danger">
            {plugin.error}
          </p>
        ) : null}
      </EmptyState>
    )
  }
  if (plugin.status !== "loaded") {
    return (
      <EmptyState
        title={`${plugin.name} is not loaded`}
        description={
          plugin.status === "disabled"
            ? "It is listed under plugins.disabled in this gateway's config, so nothing of it is mounted."
            : "It was installed since the gateway started. Restart the gateway to load it."
        }
        actionLabel="Open Marketplace"
        onAction={openMarketplace}
      />
    )
  }
  if (!plugin.ui) {
    return (
      <EmptyState
        title={`${plugin.name} has no page`}
        description="This plugin adds routes or commands to the gateway but ships no dashboard page."
        actionLabel="Open Marketplace"
        onAction={openMarketplace}
      />
    )
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <PageIntro
        title={plugin.ui.label}
        action={
          <ExternalLink
            href={plugin.ui.url}
            target="_blank"
            rel="noreferrer"
            className="text-sm text-link"
          >
            Open in a new tab
          </ExternalLink>
        }
      />
      <iframe
        title={plugin.ui.label}
        src={plugin.ui.url}
        sandbox="allow-scripts allow-forms allow-same-origin allow-popups allow-downloads"
        className="min-h-[24rem] w-full flex-1 border border-border bg-surface"
      />
    </div>
  )
}
