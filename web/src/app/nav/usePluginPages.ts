import { isDeploymentOperator } from "@/features/organization/roles"
import { useOrganizationContext } from "@/shared/api/organizations"
import { usePlugins } from "@/shared/api/plugins"
import { useSurfaces } from "@/shared/hooks/useDeployment"

/** A loaded plugin's page, as the rail and the breadcrumbs name it. */
export interface PluginPage {
  name: string
  label: string
  /** The dashboard path, which is what `pathname` compares against. */
  path: string
}

/**
 * The plugin pages this gateway serves, for the rows the registry cannot
 * declare: a registry entry is typed against the route tree and carries no
 * params, and which plugins exist is a fact about the deployment rather than
 * about the build.
 *
 * Gated the way the Marketplace row is, on the same two axes: the `plugins`
 * surface has to be hosted, and `GET /plugins` is operator-only, so a caller
 * who is not one is never asked to make a request the server refuses. Both are
 * read from what the shell already holds, so no row appears late or vanishes.
 */
export function usePluginPages(): readonly PluginPage[] {
  const hostsSurface = useSurfaces()
  const organization = useOrganizationContext()
  const enabled =
    hostsSurface("plugins") && isDeploymentOperator(organization.data)
  const plugins = usePlugins(enabled)
  if (!enabled || !plugins.data) return []
  return plugins.data.plugins.flatMap((plugin) =>
    plugin.status === "loaded" && plugin.ui
      ? [
          {
            name: plugin.name,
            label: plugin.ui.label,
            path: `/plugins/${plugin.name}`,
          },
        ]
      : [],
  )
}
