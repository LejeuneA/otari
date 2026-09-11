import type { IconType } from "react-icons"
import {
  FiActivity,
  FiBell,
  FiBook,
  FiCheckCircle,
  FiDatabase,
  FiEye,
  FiGitBranch,
  FiGlobe,
  FiLayout,
  FiLock,
  FiMessageSquare,
  FiPackage,
  FiSearch,
  FiShield,
  FiSliders,
  FiTerminal,
  FiTool,
  FiZap,
} from "react-icons/fi"

import type { PluginPageInfo } from "@/client"

/**
 * The glyphs a plugin's manifest may name, in the Feather set the rail draws
 * every other row with. A closed set on both sides: the manifest vocabulary is
 * an enum in the gateway, and a name this build does not know wears the
 * generic page glyph rather than nothing.
 */
const PLUGIN_ICONS: Record<PluginPageInfo["icon"], IconType> = {
  layout: FiLayout,
  shield: FiShield,
  activity: FiActivity,
  tool: FiTool,
  zap: FiZap,
  package: FiPackage,
  "check-circle": FiCheckCircle,
  "git-branch": FiGitBranch,
  eye: FiEye,
  bell: FiBell,
  book: FiBook,
  database: FiDatabase,
  globe: FiGlobe,
  "message-square": FiMessageSquare,
  sliders: FiSliders,
  terminal: FiTerminal,
  search: FiSearch,
  lock: FiLock,
}

export function pluginPageIcon(name: string): IconType {
  return (PLUGIN_ICONS as Record<string, IconType>)[name] ?? FiLayout
}
