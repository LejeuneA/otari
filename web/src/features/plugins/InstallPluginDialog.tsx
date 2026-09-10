import { Link as ExternalLink } from "@heroui/react"
import type { ReactNode } from "react"
import { useState } from "react"

import type { MarketplacePlugin, PluginManifestSummary } from "@/client"
import { ConfirmDialog } from "@/design-system/feedback/ConfirmDialog"
import { errorMessage } from "@/design-system/feedback/errorMessage"
import { Field } from "@/design-system/forms/Field"
import { contributionDescription } from "@/features/plugins/pluginPresentation"
import { useDescribePlugin } from "@/shared/api/plugins"

/**
 * The confirm in front of writing plugin code onto the gateway.
 *
 * Two shapes, decided by `verified`. A plugin from mozilla.ai's index has been
 * reviewed, so a plain confirm is enough. Anything else has not, and it will
 * run inside the gateway with everything the gateway can reach, so the dialog
 * says that in plain words and holds the confirm until the operator has typed
 * the thing they are about to install: the repository, or the file's name.
 * Typing it is not a hurdle for its own sake; it is the one moment the name is
 * read rather than clicked past.
 *
 * For a marketplace entry the dialog also says what the plugin declares it
 * adds, from the listing when it carried the manifest and from the repository
 * otherwise. A manifest that cannot be read does not block the install: the
 * gateway refuses a plugin that does more than it declared, so the risk the
 * warning names is the whole of it.
 */
export function InstallPluginDialog({
  isOpen,
  onOpenChange,
  heading,
  summary,
  entry,
  verified,
  confirmation,
  confirmationLabel,
  confirmLabel,
  isPending,
  error,
  onConfirm,
}: {
  isOpen: boolean
  onOpenChange: (open: boolean) => void
  heading: string
  /** What is about to be installed, and from where. */
  summary: ReactNode
  /** The marketplace entry, when installing from one; an upload has none. */
  entry?: MarketplacePlugin
  verified: boolean
  /** The exact text an unverified install has to be confirmed with. */
  confirmation: string
  /** What that text is: "Repository", "File name". */
  confirmationLabel: string
  confirmLabel: string
  isPending: boolean
  error?: unknown
  onConfirm: () => void
}) {
  const [typed, setTyped] = useState("")
  const confirmed = verified || typed.trim() === confirmation

  return (
    <ConfirmDialog
      isOpen={isOpen}
      onOpenChange={(open) => {
        // The typed name belongs to one opening. A second dialog for a second
        // plugin must start empty, or the name from the first confirms it.
        if (!open) setTyped("")
        onOpenChange(open)
      }}
      heading={heading}
      confirmVariant={verified ? "primary" : "danger"}
      confirmLabel={confirmLabel}
      isConfirmDisabled={!confirmed}
      isPending={isPending}
      error={error}
      onConfirm={onConfirm}
      body={
        <div className="flex flex-col gap-4">
          <p>{summary}</p>
          {entry ? <ManifestBlock entry={entry} isOpen={isOpen} /> : null}
          {verified ? (
            <p>It loads on the next restart of the gateway.</p>
          ) : (
            <>
              <p className="text-danger">
                This installs code from a third-party repository that mozilla.ai
                has not reviewed. It runs inside the gateway with access to
                everything the gateway can reach: provider keys, the database,
                and every request that passes through.
              </p>
              <Field
                label={`Type ${confirmation} to confirm`}
                value={typed}
                onChange={setTyped}
                placeholder={confirmation}
                autoFocus
                description={`The ${confirmationLabel.toLowerCase()}, as shown above.`}
              />
            </>
          )}
        </div>
      }
    />
  )
}

/** What the plugin declares, or why that could not be read. */
function ManifestBlock({
  entry,
  isOpen,
}: {
  entry: MarketplacePlugin
  isOpen: boolean
}) {
  const needsDescribe = isOpen && !entry.manifest
  const describe = useDescribePlugin(entry.repo, entry.ref, needsDescribe)
  const manifest = entry.manifest ?? (needsDescribe ? describe.data : undefined)

  if (manifest) {
    return <ManifestSummary manifest={manifest} />
  }
  if (needsDescribe && describe.isPending) {
    return <p className="text-subtle">Reading the plugin's manifest…</p>
  }
  if (needsDescribe && describe.error) {
    return (
      <div className="flex flex-col gap-1">
        <p className="text-danger">{errorMessage(describe.error)}</p>
        <p>
          The plugin could not be described before install. You can still
          install it; the gateway refuses a plugin that adds more than its
          manifest declares.
        </p>
      </div>
    )
  }
  return null
}

function ManifestSummary({ manifest }: { manifest: PluginManifestSummary }) {
  return (
    <div className="flex flex-col gap-2">
      <p className="text-emphasis">What it adds</p>
      {manifest.contributes.length > 0 ? (
        <ul className="list-disc flex flex-col gap-1 pl-5">
          {manifest.contributes.map((contribution) => (
            <li key={contribution}>
              {contributionDescription(contribution, manifest.name)}
            </li>
          ))}
        </ul>
      ) : (
        <p>Nothing declared.</p>
      )}
      {manifest.config_keys.length > 0 ? (
        <p>
          Reads these config keys:{" "}
          {manifest.config_keys.map((key, index) => (
            <span key={key}>
              {index > 0 ? ", " : null}
              <code>{key}</code>
            </span>
          ))}
        </p>
      ) : null}
      {manifest.getting_started ? (
        <p>
          <ExternalLink
            href={manifest.getting_started}
            target="_blank"
            rel="noreferrer"
            className="text-link"
          >
            Getting started
          </ExternalLink>
        </p>
      ) : null}
    </div>
  )
}
