import { useState } from "react"

import type { InstalledPlugin, MarketplacePlugin } from "@/client"
import { RefreshButton } from "@/design-system/actions/RefreshButton"
import { ConfirmDialog } from "@/design-system/feedback/ConfirmDialog"
import { ErrorBanner } from "@/design-system/feedback/ErrorBanner"
import { InfoBanner } from "@/design-system/feedback/InfoBanner"
import { PageLoading } from "@/design-system/feedback/PageLoading"
import { PageIntro } from "@/design-system/layout/PageIntro"
import { Tab, TabRow } from "@/design-system/navigation/TabRow"
import { InstalledPluginsSection } from "@/features/plugins/InstalledPluginsSection"
import { InstallPluginDialog } from "@/features/plugins/InstallPluginDialog"
import { MarketplaceSection } from "@/features/plugins/MarketplaceSection"
import { UploadPluginCard } from "@/features/plugins/UploadPluginCard"
import {
  useInstallPlugin,
  useMarketplace,
  usePlugins,
  useRemovePlugin,
  useUploadPlugin,
} from "@/shared/api/plugins"

type MarketplaceTab = "installed" | "verified" | "community"

/** What the confirm dialog is about: a repository, or a file from disk. */
type PendingInstall =
  | { kind: "repo"; entry: MarketplacePlugin }
  | { kind: "upload"; file: File }

/**
 * Plugins: what this gateway loaded, what it could install, and the two ways
 * to install one.
 *
 * Installing is gated twice. The server refuses every write while
 * `plugins.allow_install` is off, and the page disables the same controls
 * rather than hiding them, with a banner naming the config line, so an
 * operator learns what to turn on instead of wondering where the button went.
 * The confirm dialog is the second gate, and `InstallPluginDialog` says why
 * an unverified install asks for the name to be typed.
 */
export function MarketplacePage() {
  const plugins = usePlugins()
  const [refresh, setRefresh] = useState(false)
  const marketplace = useMarketplace(refresh)
  const install = useInstallPlugin()
  const upload = useUploadPlugin()
  const remove = useRemovePlugin()

  const [tab, setTab] = useState<MarketplaceTab>("installed")
  const [pending, setPending] = useState<PendingInstall>()
  const [pendingRemove, setPendingRemove] = useState<InstalledPlugin>()

  // Both responses carry the flag; either is enough, and while neither has
  // answered the controls stay disabled, which is the safe direction.
  const installAllowed =
    plugins.data?.install_allowed ?? marketplace.data?.install_allowed ?? false
  const installed = plugins.data?.plugins ?? []
  const verified = marketplace.data?.verified ?? []
  const community = marketplace.data?.community ?? []

  // Each opener clears the mutation whose error the dialog shows, so a refusal
  // from the last attempt does not greet the next one.
  const openInstall = (entry: MarketplacePlugin) => {
    install.reset()
    setPending({ kind: "repo", entry })
  }
  const openUpload = (file: File) => {
    upload.reset()
    setPending({ kind: "upload", file })
  }
  const openRemove = (plugin: InstalledPlugin) => {
    remove.reset()
    setPendingRemove(plugin)
  }

  const confirmInstall = () => {
    if (!pending) return
    const onSuccess = () => {
      setPending(undefined)
      setTab("installed")
    }
    if (pending.kind === "repo") {
      install.mutate(
        { repo: pending.entry.repo, ref: pending.entry.ref ?? null },
        { onSuccess },
      )
      return
    }
    upload.mutate(pending.file, { onSuccess })
  }

  const refreshMarketplace = () => {
    // The first press switches the query onto the uncached listing, which is
    // itself a fetch; every press after that re-runs it.
    if (refresh) void marketplace.refetch()
    else setRefresh(true)
  }

  const marketplaceLoading = marketplace.isPending && !marketplace.data
  const marketplaceErrors = marketplace.data?.errors ?? []

  return (
    <div className="flex flex-col gap-6">
      <PageIntro
        title="Marketplace"
        action={
          <RefreshButton
            onRefresh={refreshMarketplace}
            isFetching={marketplace.isFetching}
            updatedAt={marketplace.dataUpdatedAt}
          />
        }
      >
        Plugins add routes, commands, and pages to this gateway. Installed ones
        load at startup; a plugin from the marketplace or an archive is unpacked
        into the plugins directory and loads on the next restart.
      </PageIntro>

      {plugins.data?.restart_required ? (
        <InfoBanner tone="warning">
          A plugin was installed or removed. Restart the gateway to apply it.
        </InfoBanner>
      ) : null}

      {plugins.data && !installAllowed ? (
        <InfoBanner>
          Installing and removing plugins from the dashboard is off for this
          deployment. To turn it on, set{" "}
          <code>plugins.allow_install: true</code> in <code>config.yml</code>{" "}
          (or <code>OTARI_PLUGINS_ALLOW_INSTALL=true</code>) and restart the
          gateway.
        </InfoBanner>
      ) : null}

      {marketplaceErrors.length > 0 ? (
        <InfoBanner tone="warning">
          Part of the marketplace could not be reached:{" "}
          {marketplaceErrors.join("; ")}
        </InfoBanner>
      ) : null}

      <ErrorBanner error={plugins.error} />

      <TabRow>
        <Tab isActive={tab === "installed"} onPress={() => setTab("installed")}>
          Installed ({installed.length})
        </Tab>
        <Tab isActive={tab === "verified"} onPress={() => setTab("verified")}>
          Verified ({verified.length})
        </Tab>
        <Tab isActive={tab === "community"} onPress={() => setTab("community")}>
          Community ({community.length})
        </Tab>
      </TabRow>

      {tab === "installed" ? (
        plugins.isPending && !plugins.data ? (
          <PageLoading label="Reading installed plugins…" />
        ) : (
          <InstalledPluginsSection
            plugins={installed}
            problems={plugins.data?.problems ?? []}
            installAllowed={installAllowed}
            onRemove={openRemove}
          />
        )
      ) : marketplaceLoading ? (
        <PageLoading label="Reading the marketplace…" />
      ) : marketplace.isError && !marketplace.data ? (
        <ErrorBanner error={marketplace.error} />
      ) : (
        <MarketplaceSection
          entries={tab === "verified" ? verified : community}
          verified={tab === "verified"}
          topic={marketplace.data?.topic ?? "otari-plugin"}
          installAllowed={installAllowed}
          onInstall={openInstall}
        />
      )}

      <UploadPluginCard
        installAllowed={installAllowed}
        isPending={upload.isPending}
        onUpload={openUpload}
      />

      <InstallPluginDialog
        isOpen={pending !== undefined}
        onOpenChange={(open) => {
          if (!open) setPending(undefined)
        }}
        heading={
          pending?.kind === "upload"
            ? `Upload ${pending.file.name}`
            : `Install ${pending?.entry.name ?? "plugin"}`
        }
        summary={
          pending?.kind === "upload"
            ? `${pending.file.name} is unpacked into the plugins directory.`
            : pending
              ? `Installs ${pending.entry.name}${
                  pending.entry.version ? ` ${pending.entry.version}` : ""
                } from ${pending.entry.repo}${
                  pending.entry.ref ? ` at ${pending.entry.ref}` : ""
                }.`
              : ""
        }
        verified={pending?.kind === "repo" && pending.entry.verified}
        confirmation={
          pending?.kind === "upload"
            ? pending.file.name
            : (pending?.entry.repo ?? "")
        }
        confirmationLabel={
          pending?.kind === "upload" ? "File name" : "Repository"
        }
        confirmLabel={pending?.kind === "upload" ? "Upload" : "Install"}
        isPending={install.isPending || upload.isPending}
        error={pending?.kind === "upload" ? upload.error : install.error}
        onConfirm={confirmInstall}
      />

      <ConfirmDialog
        isOpen={pendingRemove !== undefined}
        onOpenChange={(open) => {
          if (!open) setPendingRemove(undefined)
        }}
        heading="Remove plugin"
        body={
          pendingRemove
            ? `${pendingRemove.name} is deleted from the plugins directory. Its routes, commands, and page go away on the next restart; any tables its migrations created are left in place.`
            : null
        }
        confirmLabel="Remove plugin"
        isPending={remove.isPending}
        error={remove.error}
        onConfirm={() => {
          if (!pendingRemove) return
          remove.mutate(pendingRemove.name, {
            onSuccess: () => setPendingRemove(undefined),
          })
        }}
      />
    </div>
  )
}
