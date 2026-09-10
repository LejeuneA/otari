import { useId, useState } from "react"

import { Button } from "@/design-system/actions/Button"
import { CopyField } from "@/design-system/actions/CopyField"
import { Disclosure } from "@/design-system/navigation/Disclosure"
import {
  uploadCurlCommand,
  uploadFileName,
} from "@/features/plugins/pluginPresentation"

/**
 * Install from an archive on this machine, or the same call from a shell.
 *
 * The file input is native: nothing in HeroUI picks a file, and the browser's
 * own picker is the right control for it. Choosing a file does nothing on its
 * own; Upload hands it to the page, which confirms before anything is sent,
 * because an archive from disk is as unreviewed as a community repository.
 */
export function UploadPluginCard({
  installAllowed,
  isPending,
  onUpload,
}: {
  installAllowed: boolean
  isPending: boolean
  onUpload: (file: File) => void
}) {
  const [file, setFile] = useState<File>()
  const inputId = useId()
  const origin = window.location.origin
  const command = uploadCurlCommand(origin, uploadFileName(file))

  return (
    <div className="flex flex-col gap-4 border border-border p-4">
      <div className="flex flex-col gap-1">
        <h2 className="text-title">Upload a plugin</h2>
        <p className="max-w-prose text-sm text-muted">
          A <code>.zip</code> or <code>.tar.gz</code> holding the plugin's
          package and its <code>otari-plugin.toml</code>. It is unpacked into
          the plugins directory and loads on the next restart.
        </p>
      </div>
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:gap-3">
        <label htmlFor={inputId} className="text-body">
          Archive
        </label>
        <input
          id={inputId}
          type="file"
          accept=".zip,.tar.gz,.tgz,application/zip,application/gzip"
          disabled={!installAllowed || isPending}
          onChange={(event) => setFile(event.currentTarget.files?.[0])}
          className="min-h-11 max-w-full text-sm text-muted file:mr-3 file:min-h-11 file:border file:border-border file:bg-surface file:px-3 file:text-sm file:text-foreground disabled:opacity-40"
        />
        <Button
          variant="primary"
          isDisabled={!installAllowed || file === undefined}
          isPending={isPending}
          onPress={() => {
            if (file) onUpload(file)
          }}
        >
          Upload
        </Button>
      </div>
      <Disclosure heading="Or use curl">
        <div className="flex flex-col gap-2">
          <p className="max-w-prose text-caption">
            The same install from a shell, against this gateway, with the master
            key in <code>OTARI_MASTER_KEY</code>.
          </p>
          <CopyField label="curl" value={command} multiline />
        </div>
      </Disclosure>
    </div>
  )
}
