import { Link as ExternalLink } from "@heroui/react"
import { Link } from "@tanstack/react-router"

import type { InstalledPlugin, PluginProblem } from "@/client"
import { Button } from "@/design-system/actions/Button"
import { EmptyMessage } from "@/design-system/feedback/EmptyMessage"
import { Chip } from "@/design-system/indicators/Chip"
import { SettingsGroup } from "@/design-system/layout/SettingsGroup"
import { PluginList, PluginRow } from "@/features/plugins/PluginRow"
import {
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
  onRemove,
}: {
  plugins: readonly InstalledPlugin[]
  problems: readonly PluginProblem[]
  installAllowed: boolean
  onRemove: (plugin: InstalledPlugin) => void
}) {
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
            return (
              <PluginRow
                key={plugin.name}
                title={plugin.name}
                badges={<Chip tone={status.tone}>{status.label}</Chip>}
                description={plugin.description}
                error={plugin.error}
                meta={
                  <>
                    <span>v{plugin.version}</span>
                    <span>{pluginSourceLabel(plugin.source)}</span>
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
                  </>
                }
                actions={
                  <>
                    {plugin.ui && plugin.status === "loaded" ? (
                      <Link
                        to="/plugins/$name"
                        params={{ name: plugin.name }}
                        className="text-sm text-link hover:text-link-hover"
                      >
                        Open {plugin.ui.label}
                      </Link>
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
