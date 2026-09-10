import type { ReactNode } from "react"
import { useState } from "react"

import { ConfirmDialog } from "@/design-system/feedback/ConfirmDialog"
import { Field } from "@/design-system/forms/Field"

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
 */
export function InstallPluginDialog({
  isOpen,
  onOpenChange,
  heading,
  summary,
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
