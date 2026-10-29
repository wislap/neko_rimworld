import { Alert, Button, Field, InlineError, Input, NumberInput, Stack, TextBlock, useEffect, useI18n, useLocalState, useRef, useState } from "@neko/plugin-ui"
import type { PanelServices } from "../services"
import { actionPayload, normalizedError } from "../format"

export function Autoplay({ services }: { services: PanelServices; key?: string }) {
  const { t } = useI18n()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<Error | null>(null)
  const state = services.state.autoplay || {}
  const choiceKey = services.state.session?.colony_id || "none"
  const [budgetDraft, setBudgetDraft] = useLocalState<Record<string, number | "">>("rimworld:autoplay-budget-draft", { max_requests: state.settings?.max_requests ?? 100000, max_input_tokens: state.settings?.max_input_tokens ?? 1000000000, max_cost: state.settings?.max_cost ?? "", input_price: state.settings?.input_price ?? "", output_price: state.settings?.output_price ?? "", cache_read_price: state.settings?.cache_read_price ?? "" })
  const [goal, setGoal] = useLocalState(`rimworld:autoplay-goal:${choiceKey}`, "Prioritize survival and stability.")
  const [constraints, setConstraints] = useLocalState(`rimworld:autoplay-constraints:${choiceKey}`, "No cannibalism or live organ harvesting.")
  const [goalEdited, setGoalEdited] = useLocalState(`rimworld:autoplay-goal-edited:${choiceKey}`, false)
  const [constraintsEdited, setConstraintsEdited] = useLocalState(`rimworld:autoplay-constraints-edited:${choiceKey}`, false)
  const [choicesDirty, setChoicesDirty] = useLocalState(`rimworld:autoplay-choices-dirty:${choiceKey}`, false)
  const [dirty, setDirty] = useLocalState<Record<string, boolean>>("rimworld:autoplay-budget-dirty", {})
  const latest = [...(services.state.settings_saves || [])].reverse().find(r => r.kind === "autoplay")
  const [operationId, setOperationId] = useLocalState("rimworld:autoplay-save", latest?.operation_id || "")
  const currentId = useRef(operationId); currentId.current = operationId
  const nonce = useRef(services.state.action_nonce || ""); nonce.current = services.state.action_nonce || ""
  const editRevision = useRef(0)
  const submitted = useRef<{ id: string; revision: number } | null>(null)
  const [receipt, setReceipt] = useState<any>(null)
  const [checking, setChecking] = useState(false)
  const checkingGate = useRef<string | undefined>(undefined)
  useEffect(() => {
    setBudgetDraft(previous => {
      const next = { ...previous }
      for (const [key, value] of Object.entries(state.settings || {})) if (!dirty[key] && (typeof value === "number" || value === "")) next[key] = value
      return next
    })
  }, [JSON.stringify(state.settings || {})])
  useEffect(() => {
    const choices = state.activation?.payload
    if (choicesDirty || !choices) return
    if (typeof choices.goal === "string" && typeof choices.constraints === "string" &&
        typeof choices.goal_edited === "boolean" && typeof choices.constraints_edited === "boolean") {
      setGoal(choices.goal); setConstraints(choices.constraints)
      setGoalEdited(choices.goal_edited); setConstraintsEdited(choices.constraints_edited)
    }
  }, [JSON.stringify(state.activation || null), choicesDirty])
  function validReceipt(value: any, id: string) {
    return value?.operation_id === id && value.kind === "autoplay" && ["saving", "saved", "failed", "unconfirmed"].includes(value.state)
      && (value.state !== "saved" || value.persisted === true && value.applied === true)
  }
  function acceptReceipt(value: any) {
    setReceipt(value)
    if (value.state === "saved" && submitted.current?.id === value.operation_id && submitted.current.revision === editRevision.current) {
      setDirty({})
      submitted.current = null
    }
  }
  async function readReceipt(id: string) {
    const generation = nonce.current
    if (!id || checkingGate.current === generation) return
    checkingGate.current = generation
    setChecking(true)
    try {
      const value = actionPayload(await services.call("get_settings_save", { operation_id: id }, 5000))
      if (nonce.current !== generation || currentId.current !== id) return
      if (!validReceipt(value, id)) throw new Error("receipt mismatch")
      acceptReceipt(value)
      if (value.state === "saved") await services.refresh()
    } catch {
      if (nonce.current === generation && currentId.current === id) setReceipt({ operation_id: id, kind: "autoplay", state: "unconfirmed" })
    } finally {
      if (checkingGate.current === generation) { checkingGate.current = undefined; setChecking(false) }
    }
  }
  useEffect(() => {
    const saved = currentId.current ? (services.state.settings_saves || []).find(r => validReceipt(r, currentId.current)) : latest
    if (saved) { if (!currentId.current) { currentId.current = saved.operation_id; setOperationId(saved.operation_id) }; acceptReceipt(saved) }
    else if (currentId.current) {
      setReceipt({ operation_id: currentId.current, kind: "autoplay", state: "unconfirmed" })
      void readReceipt(currentId.current)
    }
  }, [services.state.action_nonce, JSON.stringify(services.state.settings_saves || [])])
  useEffect(() => {
    if (receipt?.state !== "saving" || checking) return
    const timer = setTimeout(() => { void readReceipt(receipt.operation_id) }, 700)
    return () => clearTimeout(timer)
  }, [receipt, checking])
  async function saveBudget() {
    if (busy || receipt?.state === "saving") return
    if (["max_requests", "max_input_tokens"].some(key => dirty[key] && (typeof budgetDraft[key] !== "number" || !Number.isInteger(budgetDraft[key]) || Number(budgetDraft[key]) <= 0))) { setError(new Error(t("autoplay.invalidCounts"))); return }
    const bytes = new Uint8Array(16); crypto.getRandomValues(bytes)
    const id = Array.from(bytes, value => value.toString(16).padStart(2, "0")).join("")
    const budgets = Object.fromEntries(Object.entries(budgetDraft).filter(([key]) => dirty[key]).map(([key, value]) => [key, value === "" ? null : value]))
    submitted.current = { id, revision: editRevision.current }
    currentId.current = id; setOperationId(id); setReceipt({ operation_id: id, kind: "autoplay", state: "saving" })
    const generation = nonce.current
    setBusy(true); setError(null)
    try {
      const value = actionPayload(await services.call("begin_settings_save", { operation_id: id, kind: "autoplay", ui_nonce: generation, budgets }))
      if (nonce.current !== generation || currentId.current !== id) return
      if (!validReceipt(value, id)) throw new Error("receipt mismatch")
      acceptReceipt(value); if (value.state === "saved") await services.refresh()
    } catch { await readReceipt(id) } finally { setBusy(false) }
  }
  async function action(id: string, args: Record<string, any>) {
    setBusy(true); setError(null)
    try {
      actionPayload(await services.call(id, { ...args, ui_nonce: services.state.action_nonce }))
      if (id === "autoplay_mode" && args.enabled) setChoicesDirty(false)
      await services.refresh()
    }
    catch (e) { setError(normalizedError(e)) }
    finally { setBusy(false) }
  }
  return <Stack>
    <TextBlock text={t(state.enabled ? "autoplay.stateOn" : "autoplay.stateOff")} />
    {!state.enabled ? <Stack>
      <Field label={t("autoplay.goal")}><Input value={goal} onChange={value => { setGoal(value.slice(0, 2000)); setGoalEdited(true); setChoicesDirty(true) }} /></Field>
      <Field label={t("autoplay.constraints")}><Input value={constraints} onChange={value => { setConstraints(value.slice(0, 2000)); setConstraintsEdited(true); setChoicesDirty(true) }} /></Field>
    </Stack> : null}
    <Button disabled={busy || (!state.enabled && (!goal.trim() || !constraints.trim()))} onClick={() => action("autoplay_mode", state.enabled ? { enabled: false } : { enabled: true, goal, constraints, goal_edited: goalEdited, constraints_edited: constraintsEdited })}>{t(state.enabled ? "autoplay.stop" : "autoplay.enable")}</Button>
    {error ? <InlineError title={t("autoplay.error")} error={error} /> : null}
    {state.fault ? <Alert tone="warning">{`${t("autoplay.stopped")}: ${state.fault.kind} / ${state.fault.code}; ${t("autoplay.pause")}: ${state.fault.pause}`}</Alert> : null}
    {state.budget ? <TextBlock text={`${t("autoplay.requests")}: ${state.budget.requests}; ${t("autoplay.input")}: ${state.budget.input_tokens}; ${t("autoplay.cost")}: ${state.budget.known_cost ?? t("autoplay.unknown")}; ${t("autoplay.unknownUsage")}: ${state.budget.unknown_usage}; ${t("autoplay.unknownCost")}: ${state.budget.unknown_cost}`} /> : null}
    <Stack><TextBlock text={t("autoplay.budget")} />
      <TextBlock text={t("autoplay.budgetHelp")} />
      {Object.entries(budgetDraft).map(([key, value]) => <Field key={key} label={t(`autoplay.${key}`)}><NumberInput value={value} min={0} step={key.startsWith("max_") && key !== "max_cost" ? 1 : 0.01} onChange={v => { editRevision.current++; setDirty({ ...dirty, [key]: true }); setBudgetDraft({ ...budgetDraft, [key]: v === "" ? "" : Number(v) }) }} /></Field>)}
      <Button disabled={busy || receipt?.state === "saving" || !Object.keys(dirty).length} onClick={saveBudget}>{t("autoplay.save")}</Button>
      {receipt ? <Stack><TextBlock text={`${t("autoplay.saved")}: ${receipt.state}`} />{receipt.state !== "saved" ? <Button disabled={checking} onClick={() => readReceipt(receipt.operation_id)}>{t("autoplay.check")}</Button> : null}</Stack> : null}
    </Stack>
    <TextBlock text={t("autoplay.board")} />
    {(state.board || []).map((entry: any) => <TextBlock key={entry.entry_id} text={`${entry.kind} ${entry.entry_id} · revision ${entry.revision} · ${entry.status} · ${entry.body}`} />)}
    <TextBlock text={t("autoplay.takeovers")} />
    {(state.takeovers || []).map((entry: any) => <TextBlock key={entry.seq} text={`${entry.payload.object} / ${entry.payload.field}`} />)}
    <TextBlock text={t("autoplay.actions")} />
    {(state.actions || []).map((entry: any) => <div key={entry.seq}><TextBlock text={`${entry.mode} · ${entry.status || "unknown"} · ${entry.changeset_id || ""} · tick ${entry.tick}`} />{entry.undo_available ? <Button disabled={busy} onClick={() => action("autoplay_undo", { seq: entry.seq })}>{t("autoplay.undo")}</Button> : null}</div>)}
  </Stack>
}
