import { Link as ExternalLink } from "@heroui/react"
import { Link } from "@tanstack/react-router"
import { useState } from "react"

import type { InstalledPlugin, PluginPageInfo, PluginProblem } from "@/client"
import { Button } from "@/design-system/actions/Button"
import { EmptyMessage } from "@/design-system/feedback/EmptyMessage"
import { Chip } from "@/design-system/indicators/Chip"
import { SettingsGroup } from "@/design-system/layout/SettingsGroup"
import { PluginList, PluginRow } from "@/features/plugins/PluginRow"
import { PluginSettingsDialog } from "@/features/plugins/PluginSettingsDialog"
import {
  contributionChipLabel,
  contributionChipTone,
  pluginSourceLabel,
  pluginStatusChip,
} from "@/features/plugins/pluginPresentation"

/**
 * What this gateway discovered, loaded or not, and the candidates it could not
 * even describe.
 *
 * Only a plugin in the plugins directory can be removed from here: one
 * installed as a Python distribution came in through pip or uv and goes out the
 * same way, which the row says instead of offering a button the server would
 * refuse.
 */
export function InstalledPluginsSection({
  plugins,
  problems,
  installAllowed,
  pluginApi,
  onRemove,
}: {
  plugins: readonly InstalledPlugin[]
  problems: readonly PluginProblem[]
  installAllowed: boolean
  /** The plugin API version this gateway provides, to name a plugin that wants a newer one. */
  pluginApi?: number
  onRemove: (plugin: InstalledPlugin) => void
}) {
  const [settingsFor, setSettingsFor] = useState<string>()
  return (
    <div className="flex flex-col gap-6">
      {plugins.length === 0 ? (
        <EmptyMessage>
          No plugins installed. Pick one from the Verified or Community list, or
          upload an archive below.
        </EmptyMessage>
      ) : (
        <PluginList ariaLabel="Installed plugins">
          {plugins.map((plugin) => {
            const status = pluginStatusChip(plugin.status)
            const pages = plugin.pages ?? []
            const settings = plugin.settings ?? []
            const needsNewerApi =
              pluginApi !== undefined && plugin.plugin_api > pluginApi
            return (
              <PluginRow
                key={plugin.name}
                title={plugin.name}
                badges={
                  <>
                    <Chip tone={status.tone}>{status.label}</Chip>
                    {plugin.contributes.map((contribution) => (
                      <Chip
                        key={contribution}
                        tone={contributionChipTone(contribution)}
                      >
                        {contributionChipLabel(contribution)}
                      </Chip>
                    ))}
                  </>
                }
                description={plugin.description}
                error={plugin.error}
                meta={
                  <>
                    <span>v{plugin.version}</span>
                    <span>{pluginSourceLabel(plugin.source)}</span>
                    {needsNewerApi ? (
                      <span className="text-danger">
                        Needs plugin API {plugin.plugin_api}; this gateway
                        provides {pluginApi}
                      </span>
                    ) : (
                      <span>Plugin API {plugin.plugin_api}</span>
                    )}
                    <span>Loads in: {plugin.modes.join(", ")}</span>
                    {plugin.health ? (
                      <span
                        className={
                          plugin.health === "ok"
                            ? "text-success"
                            : "text-danger"
                        }
                      >
                        Health: {plugin.health}
                      </span>
                    ) : null}
                    {plugin.homepage ? (
                      <ExternalLink
                        href={plugin.homepage}
                        target="_blank"
                        rel="noreferrer"
                        className="text-link"
                      >
                        Homepage
                      </ExternalLink>
                    ) : null}
                    {plugin.getting_started ? (
                      <ExternalLink
                        href={plugin.getting_started}
                        target="_blank"
                        rel="noreferrer"
                        className="text-link"
                      >
                        Getting started
                      </ExternalLink>
                    ) : null}
                  </>
                }
                actions={
                  <>
                    {plugin.status === "loaded"
                      ? pages.map((page, index) => (
                          <PageLink
                            key={page.id}
                            plugin={plugin.name}
                            page={page}
                            first={index === 0}
                          />
                        ))
                      : null}
                    {plugin.status === "loaded" && settings.length > 0 ? (
                      <Button
                        size="sm"
                        onPress={() => setSettingsFor(plugin.name)}
                      >
                        Settings
                      </Button>
                    ) : null}
                    {plugin.source === "directory" ? (
                      <Button
                        size="sm"
                        isDisabled={!installAllowed}
                        onPress={() => onRemove(plugin)}
                      >
                        Remove
                      </Button>
                    ) : (
                      <span className="text-caption text-subtle">
                        Uninstall with pip or uv
                      </span>
                    )}
                  </>
                }
              />
            )
          })}
        </PluginList>
      )}

      {settingsFor !== undefined ? (
        <PluginSettingsDialog
          pluginName={settingsFor}
          isOpen
          onOpenChange={(open) => {
            if (!open) setSettingsFor(undefined)
          }}
        />
      ) : null}

      {problems.length > 0 ? (
        <SettingsGroup
          bounded
          title="Could not be read"
          description="Candidates the gateway found but could not describe. Each is skipped until its manifest or package is fixed."
        >
          {problems.map((problem) => (
            <div
              key={`${problem.source}:${problem.location}`}
              className="flex flex-col gap-0.5 px-4 py-3"
            >
              <span className="text-emphasis break-all">
                {problem.location}
              </span>
              <span className="text-caption text-subtle">
                {pluginSourceLabel(problem.source)}
              </span>
              <span className="max-w-prose break-words text-caption text-danger">
                {problem.error}
              </span>
            </div>
          ))}
        </SettingsGroup>
      ) : null}
    </div>
  )
}

/** The dashboard route that frames a page: the first page at the plugin's own path. */
function PageLink({
  plugin,
  page,
  first,
}: {
  plugin: string
  page: PluginPageInfo
  first: boolean
}) {
  const className = "text-sm text-link hover:text-link-hover"
  return first ? (
    <Link to="/plugins/$name" params={{ name: plugin }} className={className}>
      Open {page.label}
    </Link>
  ) : (
    <Link
      to="/plugins/$name/$page"
      params={{ name: plugin, page: page.id }}
      className={className}
    >
      Open {page.label}
    </Link>
  )
}
