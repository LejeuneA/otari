import { AlertDialog, Button } from "@heroui/react"
import type { ReactNode } from "react"

import { ErrorBanner } from "./ErrorBanner"

// A controlled confirm dialog for destructive bulk actions (delete a set of
// rows, etc.). Controlled via `isOpen` so a page can open it from a bulk-action
// button rather than a Trigger element.
export interface ConfirmDialogProps {
  isOpen: boolean
  onOpenChange: (open: boolean) => void
  heading: string
  body: ReactNode
  confirmLabel: string
  confirmVariant?: "danger" | "primary"
  /**
   * Holds the confirm back until the body says otherwise: a typed name that
   * has to match, for an action whose cost is out of proportion to one click.
   * Cancel stays live either way.
   */
  isConfirmDisabled?: boolean
  isPending: boolean
  error?: unknown
  onConfirm: () => void
}

export function ConfirmDialog({
  isOpen,
  onOpenChange,
  heading,
  body,
  confirmLabel,
  confirmVariant = "danger",
  isConfirmDisabled = false,
  isPending,
  error,
  onConfirm,
}: ConfirmDialogProps) {
  return (
    <AlertDialog isOpen={isOpen} onOpenChange={onOpenChange}>
      {isOpen ? (
        <AlertDialog.Backdrop
          // Dismissable: every use of this dialog puts a destructive action
          // behind a confirm, so dismissing it is the same as Cancel and costs
          // nothing. The confirm button stays the only way to proceed.
          isDismissable
          isKeyboardDismissDisabled={false}
        >
          <AlertDialog.Container placement="center" size="md">
            <AlertDialog.Dialog>
              <AlertDialog.Header>
                <AlertDialog.Heading>{heading}</AlertDialog.Heading>
              </AlertDialog.Header>
              <AlertDialog.Body className="flex flex-col gap-4">
                <div className="text-sm text-muted">{body}</div>
                <ErrorBanner error={error} />
              </AlertDialog.Body>
              <AlertDialog.Footer>
                <Button
                  variant="ghost"
                  isDisabled={isPending}
                  onPress={() => onOpenChange(false)}
                >
                  Cancel
                </Button>
                <Button
                  variant={confirmVariant}
                  isDisabled={isConfirmDisabled}
                  isPending={isPending}
                  onPress={onConfirm}
                >
                  {confirmLabel}
                </Button>
              </AlertDialog.Footer>
            </AlertDialog.Dialog>
          </AlertDialog.Container>
        </AlertDialog.Backdrop>
      ) : null}
    </AlertDialog>
  )
}
