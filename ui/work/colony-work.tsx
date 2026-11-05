import { Alert, Button, Stack, TextBlock, useEffect, useI18n, useRef, useState } from "@neko/plugin-ui"
import type { PanelServices } from "../services"
import { text } from "../format"
import { NavigationTabs } from "../components/navigation-tabs"
import type { ConstructionRenderSpec, WorkRecord } from "./colony-work-types"
import { constructionSvg } from "./construction-svg"

const knownValues = new Set(["complete", "stopped", "in_progress", "reconcile", "continue", "cancelled", "taken_over", "removed", "blueprint", "frame", "designated", "built", "unobserved", "missing", "destroyed", "user_cancelled", "model_cancelled", "user_replaced", "not_evaluated", "active", "pending", "revoked", "covered", "unsupported", "daily_only", "unavailable", "stale", "historical", "current_epoch", "observed", "partial", "failed", "passed", "inconclusive", "reference", "effective_rules", "listed", "dynamic", "error", "unknown", "warning", "members", "shared", "allowed", "protected", "scope_conflict", "uncovered", "policy_content", "choice_required", "satisfied", "shortfall", "inactive", "pending_rebind", "rejected", "registered", "changed", "expired", "exhausted", "edit", "assign", "demand_changed", "scope_changed", "resource_scope_changed", "stock_changed", "load_epoch_changed", "scope_identity_changed", "membership_incomplete", "additional_members", "protected_effect"])
const valueText = (value: unknown, t: (key: string) => string) => typeof value === "string" && knownValues.has(value) ? t(`colony.value.${value}`) : text(value, t("colony.unknown"), 500)
const quantitiesText = (value: unknown, t: (key: string) => string) => value && typeof value === "object" && !Array.isArray(value) ? Object.entries(value).map(([key, amount]) => `${key}: ${text(amount)}`).join(" · ") || t("colony.none") : t("colony.unknown")
const yesNo = (value: unknown, t: (key: string) => string) => t(value === true ? "colony.yes" : value === false ? "colony.no" : "colony.unknown")
const purposeText = (value: unknown, t: (key: string) => string) => typeof value === "string" && ["supported", "unmet", "unknown", "stale"].includes(value) ? t(`colony.purposeJudgment.${value}`) : valueText(value, t)

export function ColonyWork({ services }: { services: PanelServices }) {
  return <ColonyWorkBody services={services} />
}

function RecordTable({ headers, rows }: { headers: string[]; rows: any[][] }) {
  const { t } = useI18n()
  const [offset, setOffset] = useState(0)
  const start = Math.min(offset, Math.max(0, Math.floor((rows.length - 1) / 40) * 40))
  return rows.length ? <Stack>
    <div style={{ overflowX: "auto" }}><table style={{ width: "100%", borderCollapse: "collapse", textAlign: "left" }}>
      <thead><tr>{headers.map(label => <th key={label} scope="col" style={{ padding: "8px", borderBottom: "1px solid var(--neko-border, #8885)" }}>{label}</th>)}</tr></thead>
      <tbody>{rows.slice(start, start + 40).map((row, index) => <tr key={start + index}>{row.map((cell, column) => <td key={column} style={{ padding: "8px", verticalAlign: "top", overflowWrap: "anywhere", borderBottom: "1px solid var(--neko-border, #8883)" }}>{cell}</td>)}</tr>)}</tbody>
    </table></div>
    {rows.length > 40 ? <div><Button disabled={start === 0} onClick={() => { setOffset(start - 40) }}>{t("common.previous")}</Button><span> {start + 1}–{Math.min(start + 40, rows.length)} / {rows.length} </span><Button disabled={start + 40 >= rows.length} onClick={() => { setOffset(start + 40) }}>{t("common.next")}</Button></div> : null}
  </Stack> : <TextBlock text={t("colony.empty")} />
}

function PlanImage({ spec }: { spec?: ConstructionRenderSpec }) {
  const { t } = useI18n()
  const host = useRef<HTMLDivElement | null>(null)
  const svg = spec ? constructionSvg(spec) : null
  const label = t("colony.imageAlt")
  useEffect(() => {
    if (!host.current || !svg) return
    // Hosted JSX creates HTML elements. Parse only our own escaped renderer output
    // to obtain SVG namespace nodes; no backend SVG/HTML is ever interpreted.
    const drawing = new DOMParser().parseFromString(svg, "image/svg+xml").documentElement
    drawing.setAttribute("aria-label", label)
    drawing.setAttribute("style", "display:block;width:100%;max-height:520px")
    host.current.replaceChildren(document.importNode(drawing, true))
    return () => { host.current?.replaceChildren() }
  }, [svg, label])
  if (!spec || !svg) return <TextBlock text={t("colony.imageUnavailable")} />
  return <figure style={{ margin: 0 }}>
    <div ref={host} style={{ maxWidth: "100%", width: "640px", border: "1px solid #8993a0" }} />
    <figcaption><TextBlock text={t("colony.imageLegend")} />
      <TextBlock text={`${t("colony.map")}: ${text(spec.map_id)} · x ${spec.bounds.min_x}–${spec.bounds.min_x + spec.bounds.width - 1}, z ${spec.bounds.min_z}–${spec.bounds.min_z + spec.bounds.height - 1}`} />
      <TextBlock text={`${t("colony.sample")}: ${typeof spec.sample?.started_tick === "number" && typeof spec.sample?.finished_tick === "number" ? `${spec.sample.started_tick}–${spec.sample.finished_tick}` : text(spec.sample?.game_tick ?? spec.sample?.sampled_tick)} · ${t("colony.load")}: ${text(spec.sample?.load_epoch)} · ${t("colony.sourceHash")}: ${text(spec.source_sha256)}${spec.plan_sha256 ? ` · ${t("colony.planHash")}: ${spec.plan_sha256}` : ""}`} />
      <TextBlock text={`${t("colony.coverage")}: ${Object.entries(spec.coverage || {}).map(([key, value]) => `${key}: ${typeof value === "boolean" ? t(value ? "colony.yes" : "colony.no") : text(value)}`).join(" · ") || t("colony.unknown")}`} />
    </figcaption>
  </figure>
}

function Construction({ plans, suggestions, loadEpoch }: { plans: WorkRecord[]; suggestions: WorkRecord[]; loadEpoch?: string | null }) {
  const { t } = useI18n()
  return <Stack>
    <h3>{t("colony.suggestions")}</h3><TextBlock text={t("colony.suggestionHelp")} />
    {!suggestions.length ? <TextBlock text={t("colony.emptySuggestions")} /> : suggestions.map((suggestion, index) => <details key={suggestion.event_seq ?? index}>
      <summary>{text(suggestion.plan?.title ?? suggestion.suggestion_id, t("colony.suggestion"))} · {t("colony.suggestion")}</summary>
      <TextBlock text={text(suggestion.description ?? suggestion.reason ?? suggestion.summary, "", 8000)} />
      <TextBlock text={`${t("colony.event")}: ${text(suggestion.event_seq)} · ${t("colony.load")}: ${text(suggestion.load_epoch)}`} />
      {suggestion.load_epoch !== loadEpoch ? <Alert tone="warning">{t("colony.historicalCandidate")}</Alert> : null}
      <PlanImage spec={suggestion.render_spec} />
      <Issues issues={suggestion.render_spec?.issues || []} />
      <AttachmentRefs attachments={suggestion.attachments || []} />
    </details>)}
    <h3>{t("colony.adopted")}</h3>
    {!plans.length ? <TextBlock text={t("colony.emptyPlans")} /> : plans.map(plan => <section key={plan.plan_id} style={{ border: "1px solid var(--neko-border, #8885)", padding: "14px", borderRadius: "8px" }}>
      <Stack><h4 style={{ margin: 0 }}>{text(plan.plan?.title ?? plan.title ?? plan.plan_id)} · {t("colony.revision")} {text(plan.revision)}</h4>
        <TextBlock text={`${t("colony.adoptedPlan")}: ${text(plan.plan_id)} · ${t("colony.phase")}: ${valueText(plan.phase, t)} · ${t("colony.task")}: ${text(plan.task_id)}`} />
        <TextBlock text={`${t("colony.placement")}: ${plan.placement_complete === true ? t("colony.yes") : plan.placement_complete === false ? t("colony.no") : t("colony.unknown")} · ${t("colony.completed")}: ${plan.construction_complete === true ? t("colony.yes") : plan.construction_complete === false ? t("colony.no") : t("colony.unknown")} · ${t("colony.purpose")}: ${purposeText(plan.purpose_status, t)}`} />
        <TextBlock text={`${t("colony.fulfilled")}: ${text(plan.counts?.fulfilled)} / ${text(plan.counts?.required)} · ${t("colony.stopped")}: ${text(plan.counts?.stopped)} · ${t("colony.unknown")}: ${text(plan.counts?.unknown)}`} />
        <PurposeAssessment assessment={plan.purpose_assessment} />
        <PlanImage spec={plan.render_spec} />
        <AttachmentRefs attachments={plan.attachments || []} />
        <details><summary>{t("colony.elements")}</summary><RecordTable headers={[t("colony.element"), t("colony.intent"), t("colony.fact"), t("colony.freshness"), t("colony.reason"), t("colony.returnReceipt")]} rows={(plan.elements || []).map((row: WorkRecord) => [text(row.id), valueText(row.intent, t), `${valueText(row.stage, t)}${row.object_id ? ` · ${row.object_id}` : ""}`, row.fresh === true ? t("colony.current") : t("colony.historical"), row.intent_reason ? text(row.intent_reason) : valueText(row.stop?.reason, t), <StopScopes stop={row.stop} />])} /></details>
        {(plan.protected_targets || []).length ? <details open><summary>{t("colony.protectedTargets")}</summary><TextBlock text={t("colony.protectedTargetsHelp")} /><RecordTable headers={[t("colony.element"), t("colony.targets"), t("colony.reason"), t("colony.returnReceipt"), t("colony.event")]} rows={plan.protected_targets.map((row: WorkRecord) => [text(row.element_id), `${text(row.target?.map_id)} · ${text(row.target?.definition ?? row.target?.kind)}${row.target?.position ? ` · x ${text(row.target.position.x)}, z ${text(row.target.position.z)}` : ""}`, valueText(row.reason, t), <StopScopes stop={row} />, text(row.seq)])} /></details> : null}
        <Issues issues={plan.issues || plan.check?.issues || plan.render_spec?.issues || []} />
      </Stack>
    </section>)}
  </Stack>
}

function Issues({ issues }: { issues: WorkRecord[] }) {
  const { t } = useI18n()
  return issues.length ? <details open><summary>{t("colony.conflicts")} ({issues.length})</summary><RecordTable headers={[t("colony.severity"), t("colony.reason"), t("colony.element")]} rows={issues.map(row => [valueText(row.severity, t), text(row.reason ?? row.code, "", 1000), (row.element_ids || []).join(", ")])} /></details> : null
}

function StopScopes({ stop }: { stop?: WorkRecord }) {
  const { t } = useI18n()
  if (!stop) return <span>{t("colony.none")}</span>
  const scopes: WorkRecord[] = stop.scopes?.length ? stop.scopes : stop.scope ? [stop.scope] : []
  const returns: WorkRecord[] = stop.scope_returns || []
  const legacyReturn = !stop.scopes?.length && !!stop.scope && !!stop.released_by
  const returned = scopes.filter(scope => returns.some(row => row.object === scope.object && row.field === scope.field) || legacyReturn).length
  const cells = scopes.length > 0 && scopes.every(scope => typeof scope.field === "string" && scope.field.startsWith("area_flag:"))
  return <Stack>
    {scopes.length ? <TextBlock text={t(cells ? "colony.returnedCellCounts" : "colony.returnedScopeCounts", { total: scopes.length, returned, pending: scopes.length - returned })} /> : null}
    {stop.released_by ? <TextBlock text={`${t("colony.returnReceipt")}: ${text(stop.released_by)}`} /> : scopes.length ? <TextBlock text={t("colony.notReturned")} /> : null}
    {scopes.length ? <details><summary>{t("colony.scope")}</summary>{scopes.map((scope, index) => {
      const receipt = returns.find(row => row.object === scope.object && row.field === scope.field)?.return_seq ?? stop.released_by
      return <TextBlock key={index} text={`${text(scope.object)} / ${text(scope.field)} · ${receipt ? `${t("colony.returnReceipt")}: ${text(receipt)}` : t("colony.notReturned")}`} />
    })}</details> : !stop.released_by ? <span>{t("colony.unknown")}</span> : null}
  </Stack>
}

function PurposeAssessment({ assessment }: { assessment?: WorkRecord | null }) {
  const { t } = useI18n()
  if (!assessment) return null
  const stale = assessment.stale === true || assessment.freshness === "stale"
  const reasons = (assessment.stale_reasons || []).map((reason: string) => ["revision_changed", "load_epoch_changed", "related_facts_changed"].includes(reason) ? t(`colony.purposeStale.${reason}`) : text(reason)).join(" · ")
  return <details open><summary>{t("colony.purposeDetails")}</summary><Stack>
    <TextBlock text={t("colony.purposeAssessmentHelp")} />
    <TextBlock text={`${t("colony.assessmentSource")}: ${assessment.source === "planner_assessment" ? t("colony.plannerAssessment") : t("colony.unknown")} · ${t("colony.freshness")}: ${t(stale ? "colony.value.stale" : assessment.freshness === "current" ? "colony.current" : "colony.unknown")}`} />
    {stale ? <Alert tone="warning">{t("colony.purposeStaleHelp")}{reasons ? ` ${reasons}` : ""}</Alert> : null}
    <TextBlock text={`${t("colony.assessedGoal")}: ${text(assessment.goal, "", 20000)}`} />
    <TextBlock text={`${t("colony.recordedJudgment")}: ${purposeText(assessment.judgment, t)}`} />
    <TextBlock text={`${t("colony.assessedRevision")}: ${text(assessment.revision)} · ${t("colony.currentRevision")}: ${text(assessment.current_revision)} · ${t("colony.assessedLoad")}: ${text(assessment.load_epoch)} · ${t("colony.currentLoad")}: ${text(assessment.current_load_epoch)}`} />
    <TextBlock text={`${t("colony.assessedTick")}: ${text(assessment.assessed_tick)} · ${t("colony.event")}: ${text(assessment.assessed_seq)} · ${t("colony.factsThrough")}: ${text(assessment.facts_through_seq)}`} />
    <h5 style={{ margin: 0 }}>{t("colony.purposeFindings")}</h5>
    {(assessment.findings || []).map((finding: string, index: number) => <TextBlock key={index} text={finding} />)}
    <TextBlock text={`${t("colony.purposeEvidence")}: ${(assessment.evidence_ids || []).join(", ") || t("colony.none")}`} />
    {(assessment.evidence_refs || []).length ? <RecordTable headers={[t("colony.observation"), t("colony.scope"), t("colony.sample"), t("colony.sourceHash")]} rows={assessment.evidence_refs.map((reference: WorkRecord) => [<span>{text(reference.observation_id)}<br />{text(reference.operation)}</span>, `${t("colony.map")}: ${text(reference.map_id)} · ${t("colony.load")}: ${text(reference.sample?.load_epoch)}`, typeof reference.sample?.started_tick === "number" && typeof reference.sample?.finished_tick === "number" ? `${reference.sample.started_tick}–${reference.sample.finished_tick}` : text(reference.game_tick), text(reference.sha256)])} /> : null}
  </Stack></details>
}

function AttachmentRefs({ attachments }: { attachments: WorkRecord[] }) {
  const { t } = useI18n()
  return attachments.length ? <details><summary>{t("colony.attachments")} ({attachments.length})</summary><TextBlock text={t("colony.attachmentHelp")} />
    <RecordTable headers={[t("colony.attachment"), t("colony.imageHash"), t("colony.scope")]} rows={attachments.map(row => [<span>{text(row.evidence_id ?? row.reference_id ?? row.media_id)}<br />{text(row.mime_type)} · {text(row.width)} × {text(row.height)}</span>, text(row.sha256), `${t("colony.map")}: ${text(row.metadata?.map_id)} · ${t("colony.load")}: ${text(row.metadata?.load_epoch)} · ${t("colony.observation")}: ${text(row.metadata?.observation_id)} · ${t("colony.revision")}: ${text(row.metadata?.revision)}`])} />
  </details> : null
}

function Operations({ resources, policies }: { resources: WorkRecord[]; policies: WorkRecord[] }) {
  const { t } = useI18n()
  return <Stack><h3>{t("colony.resources")}</h3><TextBlock text={t("colony.resourceHelp")} />
    {resources.map((decision, index) => <details key={decision.allocation_id ?? index} open><summary>{text(decision.allocation_id)} · {t("colony.revision")} {text(decision.revision)}</summary>
      <TextBlock text={text(decision.reason, "", 4000)} />
      <ResourceSnapshot snapshot={decision.snapshot} title={t("colony.available")} />
      <ResourceSnapshot snapshot={decision.current_snapshot} title={t("colony.latestResourceSample")} />
      <TextBlock text={`${t("colony.freshness")}: ${valueText(decision.evidence_status, t)}`} />
      {decision.evidence_status === "historical" ? <Alert tone="warning">{t("colony.historicalResources")}</Alert> : null}
      <TextBlock text={t("colony.resourceScopeHelp")} />
      <details><summary>{t("colony.currentDemands")}</summary><RecordTable headers={[t("colony.demand"), t("colony.revision"), t("colony.window"), t("colony.relation"), t("colony.required")]} rows={(decision.current_demands || []).map((row: WorkRecord) => [text(row.id), text(row.revision), text(row.window), `${t(`colony.relation.${row.relation || "competing"}`)}${row.group_id ? ` · ${row.group_id}` : ""}`, quantitiesText(row.resources, t)])} /></details>
      <h4>{t("colony.independent")}</h4><ResourceReadings readings={decision.assessment?.independent || []} />
      <h4>{t("colony.joint")}</h4><ResourceReadings readings={decision.assessment?.joint || []} joint />
      <h4>{t("colony.selected")}</h4>
      {decision.assessment?.selected ? <Stack>
        {decision.assessment.selected.stale ? <Alert tone="warning">{t("colony.allocationStale")}</Alert> : null}
        <TextBlock text={`${t("colony.window")}: ${text(decision.assessment.selected.window)} · ${t("colony.satisfied")}: ${yesNo(decision.assessment.selected.current_stock?.satisfied, t)} · ${t("colony.shortfalls")}: ${quantitiesText(decision.assessment.selected.current_stock?.shortfalls, t)}`} />
        <RecordTable headers={[t("colony.demand"), t("colony.share"), t("colony.remaining"), t("colony.satisfied")]} rows={(decision.assessment.selected.demands || []).map((row: WorkRecord) => [text(row.demand_id), quantitiesText(row.share, t), quantitiesText(row.remaining, t), yesNo(row.satisfied, t)])} />
      </Stack> : <TextBlock text={t("colony.unknown")} />}
      {(decision.assessment?.competition || []).length ? <details><summary>{t("colony.competition")}</summary><RecordTable headers={[t("colony.resource"), t("colony.window"), t("colony.demand"), t("colony.shortfalls")]} rows={decision.assessment.competition.map((row: WorkRecord) => [text(row.resource), text(row.window), (row.demand_ids || []).join(", "), text(row.shortfall, t("colony.unknown"))])} /></details> : null}
      {(decision.assessment?.discrepancies || []).length ? <details open><summary>{t("colony.discrepancies")}</summary>{decision.assessment.discrepancies.map((row: WorkRecord, index: number) => <TextBlock key={index} text={`${valueText(row.kind, t)} · ${text(row.demand_id ?? row.field)}`} />)}</details> : null}
    </details>)}
    {!resources.length ? <TextBlock text={t("colony.emptyResources")} /> : null}
    <h3>{t("colony.policies")}</h3><TextBlock text={t("colony.policyHelp")} />
    {policies.map(row => <details key={row.impact_id} open><summary>{text(row.permission?.policy_id)} · {t("colony.revision")} {text(row.revision)} · {valueText(row.assessment?.status, t)}</summary>
      <TextBlock text={t("colony.policySnapshot")} />
      <TextBlock text={`${t("colony.scope")}: ${valueText(row.permission?.mode, t)} (${text(row.permission?.scope_id)}) · ${t("colony.action")}: ${valueText(row.action, t)}`} />
      <TextBlock text={`${t("colony.permittedMembers")}: ${(row.permission?.members || []).join(", ") || t("colony.none")} · ${t("colony.actualMembers")}: ${(row.assessment?.actual_members || []).join(", ") || t("colony.none")} · ${t("colony.affected")}: ${(row.assessment?.affected_members || []).join(", ") || t("colony.none")}`} />
      <TextBlock text={`${t("colony.additionalMembers")}: ${(row.assessment?.extra_members || []).join(", ") || t("colony.none")} · ${t("colony.reason")}: ${(row.assessment?.reasons || []).map((reason: string) => valueText(reason, t)).join(" · ") || t("colony.none")}`} />
      <TextBlock text={t("colony.applyTimeCheck")} />
      <RecordTable headers={[t("colony.protection"), t("colony.policy"), t("colony.affected")]} rows={(row.protections || []).map((protection: WorkRecord) => [valueText(protection.kind, t), text(protection.policy_id), text(protection.pawn_id)])} />
    </details>)}
    {!policies.length ? <TextBlock text={t("colony.empty")} /> : null}
  </Stack>
}

function ResourceReadings({ readings, joint = false }: { readings: WorkRecord[]; joint?: boolean }) {
  const { t } = useI18n()
  return <RecordTable headers={[t(joint ? "colony.window" : "colony.demand"), t("colony.required"), t("colony.shortfalls"), t("colony.unknown"), t("colony.satisfied")]} rows={readings.map(row => [joint ? text(row.window) : `${text(row.demand_id)} · ${t("colony.revision")} ${text(row.revision)} · ${text(row.window)}`, quantitiesText(row.required, t), quantitiesText(row.shortfalls, t), (row.unknown || []).join(", ") || t("colony.none"), `${yesNo(row.satisfied, t)}${row.status ? ` · ${valueText(row.status, t)}` : ""}`])} />
}

function ResourceSnapshot({ snapshot, title }: { snapshot?: WorkRecord; title: string }) {
  const { t } = useI18n()
  return <Stack><TextBlock text={`${title}: ${quantitiesText(snapshot?.quantities, t)} · ${t("colony.sample")}: ${text(snapshot?.game_tick)} · ${t("colony.load")}: ${text(snapshot?.load_epoch)} · ${t("colony.coverage")}: ${yesNo(snapshot?.complete, t)}`} />
    <TextBlock text={`${t("colony.map")}: ${text(snapshot?.map_id)} · ${t("colony.resourceSources")}: ${(snapshot?.scope?.selected_sources || []).map((source: string) => ["spawned_ground", "player_pawn_held", "other_held", "trader_stock"].includes(source) ? t(`colony.source.${source}`) : text(source)).join(" · ") || t("colony.unknown")}`} />
  </Stack>
}

function Observations({ intents, coverage }: { intents: WorkRecord[]; coverage: WorkRecord[] }) {
  const { t } = useI18n()
  return <Stack><h3>{t("colony.observations")}</h3><TextBlock text={t("colony.observationHelp")} />
    {intents.map(row => <details key={row.observation_id} open><summary>{text(row.observation_id)} · {t("colony.revision")} {text(row.revision)} · {valueText(row.coverage_status, t)}</summary>
      <TextBlock text={`${t("colony.task")}: ${text(row.task_id)} · ${t("colony.load")}: ${text(row.binding?.load_epoch)}`} />
      <RecordTable headers={[t("colony.condition"), t("colony.targets"), t("colony.interval"), t("colony.expires"), t("colony.sampleLimit")]} rows={(row.conditions || []).map((condition: WorkRecord) => [text(condition.id) + " · " + text(condition.kind), (condition.target_ids || []).join(", "), text(condition.sample_interval_ticks), text(condition.expires_at_tick), text(condition.max_samples)])} />
      {(row.latest_facts || []).length ? <RecordTable headers={[t("colony.condition"), t("colony.state"), t("colony.sample"), t("colony.freshness"), t("colony.receipt")]} rows={row.latest_facts.map((fact: WorkRecord) => {
        const current = coverage.find(item => item.observation_id === row.observation_id)
        const matches = current?.receipt_id && current.receipt_id === fact.receipt_id && ["colony_id", "load_epoch", "connection_epoch"].every(key => typeof current.binding?.[key] === "string" && current.binding[key] === fact.binding?.[key])
        return [text(fact.condition_id), valueText(fact.status, t), text(fact.sample_tick), matches ? t("colony.current") : t("colony.historical"), text(fact.receipt_id)]
      })} /> : null}
    </details>)}
    {!intents.length ? <TextBlock text={t("colony.empty")} /> : null}
    <h3>{t("colony.coverage")}</h3>
    <RecordTable headers={[t("colony.observation"), t("colony.coverage"), t("colony.coveredConditions"), t("colony.pending"), t("colony.conditionStates"), t("colony.receipt")]} rows={coverage.map(row => [text(row.observation_id), valueText(row.status, t), (row.covered_condition_ids || []).join(", ") || t("colony.none"), (row.uncovered_condition_ids || []).join(", ") || t("colony.none"), Object.entries(row.condition_states || {}).map(([id, status]) => `${id}: ${valueText(status, t)}`).join(" · ") || t("colony.none"), `${text(row.receipt_id)}${row.ack?.reason ? ` · ${row.ack.reason}` : ""}`])} />
  </Stack>
}

function ColonyWorkBody({ services }: { services: PanelServices }) {
  const { t } = useI18n()
  const [view, setView] = useState("construction")
  const work = services.state.autoplay?.colony_work
  const plans = work?.construction?.plans || []
  const history = [
    ...plans.flatMap(plan => (plan.history || []).map((row: WorkRecord) => [t("colony.construction"), text(plan.plan_id), text(row.revision), `${text(row.seq)} · ${text(row.kind)}`, text(row.load_epoch)])),
    ...(work?.resources?.decisions || []).flatMap(decision => (decision.history || []).map((row: WorkRecord) => [t("colony.resources"), text(decision.allocation_id), text(row.revision), `${text(row.seq)} · ${text(row.observation_id)}`, t("colony.unknown")])),
    ...(work?.policies?.impacts || []).flatMap(impact => (impact.history || []).map((row: WorkRecord) => [t("colony.policies"), text(impact.impact_id), text(row.revision), `${text(row.seq)} · ${valueText(row.status, t)}`, t("colony.unknown")])),
    ...(work?.observations?.intents || []).flatMap(intent => (intent.history || []).map((row: WorkRecord) => [t("colony.observations"), text(intent.observation_id), text(row.revision), `${text(row.seq)} · ${text(row.kind)}`, t("colony.unknown")])),
  ]
  const historical = !!work && (!services.state.session?.load_epoch || work.load_epoch !== services.state.session.load_epoch || services.state.session.stale === true)
  return <section aria-label={t("colony.title")}><Stack>
    <h2>{t("colony.title")}</h2><TextBlock text={t("colony.readOnlyHelp")} />
    {historical ? <Alert tone="warning">{t("colony.historicalHelp")}</Alert> : null}
    {!work || work.format !== "colony_work.v1" ? <TextBlock text={t("colony.unavailable")} /> : <NavigationTabs id="rimworld-colony-work" activeId={view} onChange={setView} items={[
      { id: "construction", label: t("colony.construction") }, { id: "operations", label: t("colony.operations") }, { id: "observations", label: t("colony.observations") }, { id: "history", label: t("colony.history") },
    ]}>
      {view === "construction" ? <Construction plans={plans} suggestions={work.suggestions || []} loadEpoch={work.load_epoch} /> : view === "operations" ? <Operations resources={work.resources?.decisions || []} policies={work.policies?.impacts || []} /> : view === "observations" ? <Observations intents={work.observations?.intents || []} coverage={work.observations?.coverage || []} /> : <RecordTable headers={[t("colony.scope"), t("colony.record"), t("colony.revision"), t("colony.event"), t("colony.load")]} rows={history} />}
    </NavigationTabs>}
  </Stack></section>
}
