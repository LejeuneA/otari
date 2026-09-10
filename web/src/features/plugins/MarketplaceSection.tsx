import { Link as ExternalLink } from "@heroui/react"
import { FiStar } from "react-icons/fi"

import type { MarketplacePlugin } from "@/client"
import { Button } from "@/design-system/actions/Button"
import { EmptyMessage } from "@/design-system/feedback/EmptyMessage"
import { Chip } from "@/design-system/indicators/Chip"
import { PluginList, PluginRow } from "@/features/plugins/PluginRow"
import { formatNumber } from "@/shared/helpers/format"

/**
 * One of the two marketplace lists. The same rows for both; what differs is the
 * badge, which is the whole point of there being two lists: a verified entry
 * has been reviewed by mozilla.ai and a community one is whatever carries the
 * GitHub topic.
 */
export function MarketplaceSection({
  entries,
  verified,
  topic,
  installAllowed,
  onInstall,
}: {
  entries: readonly MarketplacePlugin[]
  verified: boolean
  /** The GitHub topic the community list is drawn from. */
  topic: string
  installAllowed: boolean
  onInstall: (entry: MarketplacePlugin) => void
}) {
  if (entries.length === 0) {
    return (
      <EmptyMessage>
        {verified
          ? "No verified plugins are listed right now."
          : `No community plugins carry the ${topic} topic on GitHub right now.`}
      </EmptyMessage>
    )
  }

  return (
    <PluginList ariaLabel={verified ? "Verified plugins" : "Community plugins"}>
      {entries.map((entry) => (
        <PluginRow
          key={entry.repo}
          title={entry.name}
          badges={
            <>
              {verified ? (
                <Chip tone="accent">Verified by mozilla.ai</Chip>
              ) : (
                <Chip tone="warning">Unverified</Chip>
              )}
              {entry.installed ? <Chip>Installed</Chip> : null}
            </>
          }
          description={entry.description}
          meta={
            <>
              {entry.version ? <span>v{entry.version}</span> : null}
              {entry.stars != null ? (
                <span className="inline-flex items-center gap-1">
                  <FiStar aria-hidden="true" className="size-3" />
                  {formatNumber(entry.stars)}
                  <span className="sr-only"> stars</span>
                </span>
              ) : null}
              <ExternalLink
                href={entry.url}
                target="_blank"
                rel="noreferrer"
                className="text-link"
              >
                {entry.repo}
              </ExternalLink>
            </>
          }
          actions={
            entry.installed ? null : (
              <Button
                size="sm"
                variant={verified ? "primary" : "ghost"}
                isDisabled={!installAllowed}
                onPress={() => onInstall(entry)}
              >
                Install
              </Button>
            )
          }
        />
      ))}
    </PluginList>
  )
}
