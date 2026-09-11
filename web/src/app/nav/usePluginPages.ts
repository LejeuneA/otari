import type { IconType } from "react-icons"
import { pluginPageIcon } from "@/app/nav/pluginIcons"
import type { PluginPageInfo } from "@/client"
import { usePluginPages as usePluginPagesQuery } from "@/shared/api/plugins"
import { useSurfaces } from "@/shared/hooks/useDeployment"

/** A loaded plugin's page, as the rail and the breadcrumbs name it. */
export interface PluginPage {
  id: string
  label: string
  /** The dashboard path, which is what `pathname` compares against. */
  path: string
  icon: IconType
  section: PluginPageInfo["section"]
  parent: PluginPageInfo["parent"]
  order: number
  audience: PluginPageInfo["audience"]
}

/**
 * The plugin pages this gateway serves, for the rows the registry cannot
 * declare: a registry entry is typed against the route tree and carries no
 * params, and which plugins exist is a fact about the deployment rather than
 * about the build.
 *
 * Gated on the `plugins` surface only. The caller axis is the server's:
 * `GET /plugins/pages` answers every signed-in session with the pages that
 * caller may see, so a member gets a plugin's member pages without the rail
 * knowing the plugin exists. Sorted by the manifest's `order`, then by the
 * sequence the plugins were loaded in, which is what the server already
 * returns.
 */
export function usePluginPages(): readonly PluginPage[] {
  const hostsSurface = useSurfaces()
  const enabled = hostsSurface("plugins")
  const pages = usePluginPagesQuery(enabled)
  if (!enabled || !pages.data) return []
  // A gateway older than the pages route answers something else at its path;
  // the rail then shows no plugin rows rather than nothing at all.
  return (pages.data.pages ?? [])
    .map((page, index) => ({ page, index }))
    .sort((a, b) => a.page.order - b.page.order || a.index - b.index)
    .map(({ page }) => ({
      id: page.id,
      label: page.label,
      path: page.path,
      icon: pluginPageIcon(page.icon),
      section: page.section,
      parent: page.parent ?? null,
      order: page.order,
      audience: page.audience,
    }))
}

/** The plugin rows that go in a rail section, by the section's registry id. */
export function pluginPagesForSection(
  pages: readonly PluginPage[],
  sectionId: string,
): PluginPage[] {
  const section = SECTION_BY_ID[sectionId]
  if (section === undefined) return []
  return pages.filter(
    (page) => page.parent === null && page.section === section,
  )
}

/** The plugin rows that nest under a rail item that has children. */
export function pluginPagesForParent(
  pages: readonly PluginPage[],
  parent: PluginPageInfo["parent"],
): PluginPage[] {
  if (parent === null || parent === undefined) return []
  return pages.filter((page) => page.parent === parent)
}

// The manifest names sections by what the rail calls them on screen; the
// registry ids are what the shell keys on, and "Build" has kept the id it was
// registered under.
const SECTION_BY_ID: Record<string, PluginPageInfo["section"]> = {
  observe: "observe",
  gateway: "build",
  access: "access",
  extend: "extend",
}
