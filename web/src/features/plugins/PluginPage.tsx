import { Link as ExternalLink } from "@heroui/react"
import { useNavigate, useParams } from "@tanstack/react-router"
import { useEffect, useRef, useState } from "react"

import type { PluginPageInfo } from "@/client"
import { Button } from "@/design-system/actions/Button"
import { EmptyState } from "@/design-system/feedback/EmptyState"
import { ErrorBanner } from "@/design-system/feedback/ErrorBanner"
import { PageLoading } from "@/design-system/feedback/PageLoading"
import { PageIntro } from "@/design-system/layout/PageIntro"
import { usePlugins } from "@/shared/api/plugins"

/** What the frame may ask of the dashboard, and what the dashboard tells it. */
type BridgeMessage =
  | { type: "otari:navigate"; to: string }
  | {
      type: "otari:toast"
      title: string
      description?: string
      variant?: "success" | "danger"
    }

interface Notice {
  title: string
  description?: string
  variant: "success" | "danger" | "info"
}

function currentTheme(): "light" | "dark" {
  const root = document.documentElement
  return root.dataset.theme === "dark" || root.classList.contains("dark")
    ? "dark"
    : "light"
}

function isBridgeMessage(value: unknown): value is BridgeMessage {
  if (typeof value !== "object" || value === null) return false
  const message = value as { type?: unknown; to?: unknown; title?: unknown }
  if (message.type === "otari:navigate") return typeof message.to === "string"
  if (message.type === "otari:toast") return typeof message.title === "string"
  return false
}

/**
 * A plugin's own page, framed.
 *
 * The gateway serves the plugin's static files at the page's `url`, on this
 * origin, so the frame authenticates with the same session cookie the
 * dashboard holds and needs nothing passed in. The `sandbox` attribute keeps
 * scripts, forms, and same-origin access, which is what a page that calls the
 * plugin's own API needs; with same-origin access granted it is not a trust
 * boundary, and it is not meant as one: the plugin already runs inside the
 * gateway. It only keeps a page from navigating the top window or locking the
 * pointer by accident.
 *
 * The frame is what scrolls: the page fills the content area and hands the
 * height to the frame, so an operator scrolls the plugin, not the dashboard
 * around it.
 *
 * A small `postMessage` bridge runs both ways. The dashboard tells the frame
 * which theme it wears, on load and on every change, so the page need not
 * reach into the parent document to find out. The frame may ask the dashboard
 * to navigate, or to show a notice; both are taken only from this frame's own
 * window, and a navigation only to a dashboard path.
 */
export function PluginPage() {
  // Loose, because this component is mounted at the route rather than
  // importing it (a route file exports `Route` and nothing else), and a strict
  // read would need the route's own api.
  const { name, page: pageId } = useParams({ strict: false })
  const navigate = useNavigate()
  const plugins = usePlugins()
  const frameRef = useRef<HTMLIFrameElement>(null)
  const [notice, setNotice] = useState<Notice>()

  // The frame's window is only known once the frame is mounted, which is after
  // the early returns below, so the bridge is wired in an effect keyed on the
  // page it frames rather than on mount.
  const plugin = plugins.data?.plugins.find((one) => one.name === name)
  const pages = plugin?.pages ?? []
  const page: PluginPageInfo | undefined =
    pageId === undefined ? pages[0] : pages.find((one) => one.id === pageId)
  const frameUrl = page?.url

  useEffect(() => {
    const frame = frameRef.current
    if (!frame || !frameUrl) return
    const origin = window.location.origin
    const sendTheme = () => {
      frame.contentWindow?.postMessage(
        { type: "otari:theme", theme: currentTheme() },
        origin,
      )
    }
    // Sent once the page has loaded, since a message posted before that goes
    // to a document that is not listening yet, and again on every theme flip.
    frame.addEventListener("load", sendTheme)
    const observer = new MutationObserver(sendTheme)
    observer.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["data-theme", "class"],
    })
    const onMessage = (event: MessageEvent) => {
      // Only this frame, and only this origin: the gateway serves the page on
      // the dashboard's own origin, so anything else is not the plugin.
      if (event.source !== frame.contentWindow || event.origin !== origin) {
        return
      }
      if (!isBridgeMessage(event.data)) return
      if (event.data.type === "otari:navigate") {
        if (event.data.to.startsWith("/")) {
          void navigate({ to: event.data.to })
        }
        return
      }
      setNotice({
        title: event.data.title,
        description: event.data.description,
        variant: event.data.variant ?? "info",
      })
    }
    window.addEventListener("message", onMessage)
    return () => {
      frame.removeEventListener("load", sendTheme)
      observer.disconnect()
      window.removeEventListener("message", onMessage)
    }
  }, [frameUrl, navigate])

  if (plugins.isPending && !plugins.data) {
    return <PageLoading label="Reading installed plugins…" />
  }
  if (plugins.isError && !plugins.data) {
    return <ErrorBanner error={plugins.error} />
  }

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
            ? "It is listed under plugins.disabled in this gateway's config, or its manifest names other runtime modes, so nothing of it is mounted."
            : "It was installed since the gateway started. Restart the gateway to load it."
        }
        actionLabel="Open Marketplace"
        onAction={openMarketplace}
      />
    )
  }
  if (!page) {
    return (
      <EmptyState
        title={
          pageId === undefined
            ? `${plugin.name} has no page`
            : `${plugin.name} has no page named ${pageId}`
        }
        description={
          pageId === undefined
            ? "This plugin adds routes or commands to the gateway but ships no dashboard page."
            : "The plugin's manifest names no page by that id. The Marketplace lists the pages it ships."
        }
        actionLabel="Open Marketplace"
        onAction={openMarketplace}
      />
    )
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <PageIntro
        title={page.label}
        action={
          <ExternalLink
            href={page.url}
            target="_blank"
            rel="noreferrer"
            className="text-sm text-link"
          >
            Open in a new tab
          </ExternalLink>
        }
      />
      {notice ? (
        <div
          role="status"
          className={`mb-3 flex items-start gap-3 rounded-lg border px-3 py-2 text-sm ${NOTICE_CLASS[notice.variant]}`}
        >
          <div className="flex min-w-0 flex-1 flex-col">
            <span className="text-emphasis">{notice.title}</span>
            {notice.description ? (
              <span className="text-caption">{notice.description}</span>
            ) : null}
          </div>
          <Button
            size="sm"
            variant="ghost"
            aria-label="Dismiss notice"
            onPress={() => setNotice(undefined)}
          >
            Dismiss
          </Button>
        </div>
      ) : null}
      <iframe
        ref={frameRef}
        title={page.label}
        src={page.url}
        sandbox="allow-scripts allow-forms allow-same-origin allow-popups allow-popups-to-escape-sandbox allow-modals allow-downloads"
        className="min-h-[24rem] w-full flex-1 border border-border bg-surface"
      />
    </div>
  )
}

// A status word on its own subtle fill wears the status color.
const NOTICE_CLASS: Record<Notice["variant"], string> = {
  success: "border-success bg-success-subtle text-success",
  danger: "border-danger bg-danger-subtle text-danger",
  info: "border-info bg-info-subtle text-info",
}
