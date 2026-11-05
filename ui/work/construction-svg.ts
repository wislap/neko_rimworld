import type { ConstructionRenderSpec, WorkRecord } from "./colony-work-types"

const xml = (value: unknown) => String(value ?? "").replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&apos;" }[char] || char))

/** Render only the native-derived spec; arbitrary backend markup is never inserted. */
export function constructionSvg(spec: ConstructionRenderSpec): string | null {
  const b = spec?.bounds
  if (spec?.format !== "construction_render.v1" || !b || ![b.min_x, b.min_z, b.width, b.height].every(Number.isInteger) || b.width < 1 || b.height < 1 || b.width > 64 || b.height > 64) return null
  const shapes: string[] = []
  const tile = (cell: WorkRecord, fill: string, stroke = "#455267", inset = 0) => {
    if (!Number.isInteger(cell.x) || !Number.isInteger(cell.z) || cell.x < b.min_x || cell.z < b.min_z || cell.x >= b.min_x + b.width || cell.z >= b.min_z + b.height) return
    const x = cell.x - b.min_x + inset, y = b.height - 1 - (cell.z - b.min_z) + inset
    shapes.push(`<rect x="${x}" y="${y}" width="${1 - inset * 2}" height="${1 - inset * 2}" fill="${fill}" stroke="${stroke}" stroke-width="0.035"/>`)
  }
  for (const cell of spec.cells || []) tile(cell, cell.known !== true ? "url(#unknown)" : cell.walkable === false ? "#667085" : "#e6e9dd")
  for (const area of spec.areas || []) for (const cell of area.cells || []) tile(cell, "#78b6cf55", "#39758b")
  for (const object of spec.objects || []) for (const cell of object.cells || []) tile(cell, object.stage === "Blueprint" || object.stage === "blueprint" ? "#538bb8" : object.stage === "Frame" || object.stage === "frame" ? "#b89050" : "#68785b", "#304038", 0.08)
  for (const element of spec.elements || []) {
    for (const cell of element.cells || []) tile(cell, element.layer === "floor" ? "#c6ac7d99" : "#9964d1aa", "#6538a1", 0.13)
    for (const cell of element.interaction_cells || (element.interaction_cell ? [element.interaction_cell] : [])) tile(cell, "none", "#006d77", 0.26)
  }
  for (const issue of spec.issues || []) for (const cell of issue.cells || []) tile(cell, "none", issue.severity === "error" ? "#d62236" : "#d88b00", 0.035)
  const source = xml(spec.source_sha256)
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${b.width} ${b.height}" role="img"><title>${xml(spec.plan_id || spec.map_id)} ${source}</title><defs><pattern id="unknown" width="0.4" height="0.4" patternUnits="userSpaceOnUse"><rect width="0.4" height="0.4" fill="#d7dbe4"/><path d="M0 0L.4 .4" stroke="#939baa" stroke-width=".08"/></pattern></defs><rect width="${b.width}" height="${b.height}" fill="url(#unknown)"/>${shapes.join("")}</svg>`
  return svg
}
