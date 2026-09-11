import { useState } from "react"

import type { PluginSettingField } from "@/client"
import { Button } from "@/design-system/actions/Button"
import { FormDialog } from "@/design-system/feedback/FormDialog"
import { PageLoading } from "@/design-system/feedback/PageLoading"
import { Field } from "@/design-system/forms/Field"
import { SecretField } from "@/design-system/forms/SecretField"
import { TextArea } from "@/design-system/forms/TextArea"
import { Toggle } from "@/design-system/forms/Toggle"
import {
  usePluginSettings,
  useUpdatePluginSettings,
} from "@/shared/api/plugins"

/** What a set secret reads back as; sending it would overwrite the secret with the mask. */
export const SECRET_MASK = "********"

/**
 * The draft of one field, in the shape its control edits: text for every
 * type but bool, since a number, a list, and an object are typed as text and
 * parsed on submit. `reset` marks a field the operator sent back to its
 * default, which submits as `null`.
 */
interface Draft {
  text: string
  checked: boolean
  reset: boolean
}

function draftOf(field: PluginSettingField, value: unknown): Draft {
  switch (field.type) {
    case "bool":
      return { text: "", checked: value === true, reset: false }
    case "list":
      return {
        text: Array.isArray(value) ? value.map(String).join("\n") : "",
        checked: false,
        reset: false,
      }
    case "object":
      return {
        text:
          value !== null && value !== undefined
            ? JSON.stringify(value, null, 2)
            : "",
        checked: false,
        reset: false,
      }
    default:
      return {
        text: value === null || value === undefined ? "" : String(value),
        checked: false,
        reset: false,
      }
  }
}

/**
 * The value a draft submits, or an error naming why it cannot. An empty text
 * for a number is a cleared field, which submits as `null` like a reset.
 */
function parseDraft(
  field: PluginSettingField,
  draft: Draft,
): { value: unknown } | { error: string } {
  if (draft.reset) return { value: null }
  switch (field.type) {
    case "bool":
      return { value: draft.checked }
    case "int": {
      if (draft.text.trim() === "") return { value: null }
      const parsed = Number(draft.text)
      if (!Number.isInteger(parsed)) return { error: "Must be a whole number." }
      return { value: parsed }
    }
    case "float": {
      if (draft.text.trim() === "") return { value: null }
      const parsed = Number(draft.text)
      if (!Number.isFinite(parsed)) return { error: "Must be a number." }
      return { value: parsed }
    }
    case "list":
      return {
        value: draft.text
          .split("\n")
          .map((line) => line.trim())
          .filter((line) => line !== ""),
      }
    case "object": {
      if (draft.text.trim() === "") return { value: null }
      try {
        const parsed: unknown = JSON.parse(draft.text)
        if (
          typeof parsed !== "object" ||
          parsed === null ||
          Array.isArray(parsed)
        ) {
          return { error: "Must be a JSON object." }
        }
        return { value: parsed }
      } catch {
        return { error: "Must be valid JSON." }
      }
    }
    default:
      return { value: draft.text }
  }
}

/**
 * The form a plugin's manifest describes, over its live values.
 *
 * Only what changed is sent: a set secret reads back as a mask, and sending
 * the mask would overwrite the secret with eight asterisks, so an untouched
 * secret is not sent at all. A field sent as `null` goes back to what
 * config.yml or the manifest default says, which the gateway applies and the
 * plugin is told about; no restart.
 */
export function PluginSettingsDialog({
  pluginName,
  isOpen,
  onOpenChange,
}: {
  pluginName: string
  isOpen: boolean
  onOpenChange: (open: boolean) => void
}) {
  const settings = usePluginSettings(pluginName, isOpen)
  const update = useUpdatePluginSettings(pluginName)
  // Keyed by the response identity so a reopen after a save starts from the
  // saved values rather than from the edits of the previous opening.
  const [drafts, setDrafts] = useState<Record<string, Draft>>({})
  const [errors, setErrors] = useState<Record<string, string>>({})
  const [seeded, setSeeded] = useState<unknown>()

  const fields = settings.data?.fields ?? []
  const values = settings.data?.values ?? {}
  if (settings.data !== undefined && seeded !== settings.data) {
    setSeeded(settings.data)
    setDrafts(
      Object.fromEntries(
        settings.data.fields.map((field) => [
          field.key,
          draftOf(field, settings.data.values[field.key]),
        ]),
      ),
    )
    setErrors({})
  }

  const draftFor = (field: PluginSettingField): Draft =>
    drafts[field.key] ?? draftOf(field, values[field.key])
  const setDraft = (key: string, next: Partial<Draft>) =>
    setDrafts((current) => ({
      ...current,
      [key]: {
        ...(current[key] ?? { text: "", checked: false, reset: false }),
        ...next,
      },
    }))

  const changed = fields.filter((field) => {
    const draft = draftFor(field)
    if (draft.reset) return true
    const original = draftOf(field, values[field.key])
    return field.type === "bool"
      ? draft.checked !== original.checked
      : draft.text !== original.text
  })

  const submit = () => {
    const body: Record<string, unknown> = {}
    const problems: Record<string, string> = {}
    for (const field of changed) {
      const outcome = parseDraft(field, draftFor(field))
      if ("error" in outcome) problems[field.key] = outcome.error
      else body[field.key] = outcome.value
    }
    setErrors(problems)
    if (Object.keys(problems).length > 0 || changed.length === 0) return
    update.mutate({ values: body }, { onSuccess: () => onOpenChange(false) })
  }

  return (
    <FormDialog
      isOpen={isOpen}
      onOpenChange={(open) => {
        if (!open) {
          setDrafts({})
          setErrors({})
          setSeeded(undefined)
          update.reset()
        }
        onOpenChange(open)
      }}
      title={`${pluginName} settings`}
      description="Applied at once, no restart. A value set here wins over config.yml until it is reset."
      submitLabel="Save settings"
      onSubmit={submit}
      isPending={update.isPending}
      error={update.error ?? settings.error}
      isDirty={changed.length > 0}
    >
      {settings.isPending && !settings.data ? (
        <PageLoading label="Reading settings…" />
      ) : (
        <div className="flex flex-col gap-4">
          {fields.map((field) => (
            <SettingControl
              key={field.key}
              field={field}
              draft={draftFor(field)}
              error={errors[field.key]}
              onChange={(next) =>
                setDraft(field.key, { ...next, reset: false })
              }
              onReset={() => setDraft(field.key, { reset: true })}
            />
          ))}
        </div>
      )}
    </FormDialog>
  )
}

function SettingControl({
  field,
  draft,
  error,
  onChange,
  onReset,
}: {
  field: PluginSettingField
  draft: Draft
  error?: string
  onChange: (next: Partial<Draft>) => void
  onReset: () => void
}) {
  const locked = !field.editable
  const description = locked
    ? "Set in config.yml"
    : field.description || undefined
  const control = (() => {
    if (field.type === "bool") {
      return (
        <Toggle
          label={field.key}
          isSelected={draft.reset ? field.default === true : draft.checked}
          onChange={(checked) => onChange({ checked })}
          isDisabled={locked}
        />
      )
    }
    // A locked secret is plain text of asterisks: there is nothing to reveal
    // and nothing to type, and `SecretField` has no disabled state to show.
    if (field.secret && !locked) {
      return (
        <SecretField
          label={field.key}
          value={draft.reset ? "" : draft.text}
          onChange={(text) => onChange({ text })}
          description={description}
          placeholder={
            draft.text === SECRET_MASK ? "Set; type to replace" : undefined
          }
          isInvalid={error !== undefined}
          errorMessage={error}
          reserveMessage
        />
      )
    }
    if (field.type === "list" || field.type === "object") {
      return (
        <TextArea
          label={field.key}
          value={draft.reset ? "" : draft.text}
          onChange={(text) => onChange({ text })}
          description={
            description ??
            (field.type === "list" ? "One item per line." : "A JSON object.")
          }
          rows={4}
          isDisabled={locked}
          isInvalid={error !== undefined}
          errorMessage={error}
          reserveMessage
        />
      )
    }
    return (
      <Field
        label={field.key}
        value={draft.reset ? "" : draft.text}
        onChange={(text) => onChange({ text })}
        description={description}
        isDisabled={locked}
        isInvalid={error !== undefined}
        errorMessage={error}
        reserveMessage
      />
    )
  })()
  return (
    <div className="flex flex-col gap-1">
      {control}
      {field.type === "bool" && description ? (
        <span className="text-caption text-subtle">{description}</span>
      ) : null}
      {!locked ? (
        <div className="flex items-center gap-2 text-caption text-subtle">
          {draft.reset ? (
            <span>Resets to the default on save.</span>
          ) : (
            <Button size="sm" variant="ghost" onPress={onReset}>
              Reset {field.key} to default
            </Button>
          )}
        </div>
      ) : null}
    </div>
  )
}
