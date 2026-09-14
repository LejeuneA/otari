import type { PluginContribution, PluginStatus } from "@/client"
import type { ChipTone } from "@/design-system/indicators/Chip"
import { API_ROOT } from "@/shared/api/client"

/** What a plugin's status is called on screen, and the fill it wears. */
export function pluginStatusChip(status: PluginStatus): {
  tone: ChipTone
  label: string
} {
  switch (status) {
    case "loaded":
      return { tone: "success", label: "Loaded" }
    case "failed":
      return { tone: "danger", label: "Failed" }
    case "disabled":
      return { tone: "neutral", label: "Disabled" }
    case "pending_restart":
      return { tone: "warning", label: "Restart required" }
  }
}

/**
 * What one declared contribution means, spelled out for the install dialog.
 * `name` is the plugin's, because its routes mount under it. A kind this
 * dashboard does not know is one a newer gateway defined; it is shown by its
 * own word, and the gateway's `needs_newer_gateway` says what to do about it.
 */
export function contributionDescription(
  contribution: PluginContribution,
  name: string,
): string {
  switch (contribution) {
    case "routes":
      return `API routes under /api/v1/plugins/${name}`
    case "cli":
      return "otari command groups"
    case "migrations":
      return "database tables of its own"
    case "ui":
      return "a page in the dashboard"
    case "traffic":
      return "watches inference traffic passing through this gateway"
    default:
      return `${contribution} (needs a newer gateway)`
  }
}

/** The same contribution, short enough for a chip on the installed row. */
export function contributionChipLabel(
  contribution: PluginContribution,
): string {
  switch (contribution) {
    case "routes":
      return "API routes"
    case "cli":
      return "CLI"
    case "migrations":
      return "Tables"
    case "ui":
      return "Page"
    case "traffic":
      return "Watches traffic"
    default:
      return contribution
  }
}

/** Where a plugin came from, as a word. */
export function pluginSourceLabel(source: "entry_point" | "directory"): string {
  return source === "directory" ? "Plugins directory" : "Python package"
}

/** Stands in for the archive until the operator has picked one. */
export const UPLOAD_FILE_PLACEHOLDER = "otari-agent-gates.zip"

/**
 * The same upload, from a shell. Against this gateway's own origin, because
 * the management API is served from the address that served this page, and
 * with the master key left as a variable rather than a value: the command is
 * meant to be pasted, and a credential is not.
 */
export function uploadCurlCommand(origin: string, fileName: string): string {
  return [
    `curl -X POST "${origin}${API_ROOT}/plugins/upload" \\`,
    '  -H "Authorization: Bearer $OTARI_MASTER_KEY" \\',
    `  -F "file=@${fileName}"`,
  ].join("\n")
}

/** The one file name a browser reports for a picked archive, or the placeholder. */
export function uploadFileName(file: File | undefined): string {
  return file?.name || UPLOAD_FILE_PLACEHOLDER
}
