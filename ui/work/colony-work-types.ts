/** Read-only journal projections. A displayed plan never authorizes a game write. */
export type WorkRecord = Record<string, any>

export type ConstructionRenderSpec = {
  format: "construction_render.v1"
  map_id?: number | string
  bounds: { min_x: number; min_z: number; width: number; height: number }
  sample?: WorkRecord
  source_sha256?: string
  plan_sha256?: string
  plan_id?: string
  revision?: number
  coverage?: WorkRecord
  cells?: WorkRecord[]
  objects?: WorkRecord[]
  elements?: WorkRecord[]
  areas?: WorkRecord[]
  issues?: WorkRecord[]
}

export type ColonyWorkState = {
  format: "colony_work.v1"
  load_epoch?: string | null
  construction?: { plans?: WorkRecord[] }
  resources?: { decisions?: WorkRecord[] }
  policies?: { impacts?: WorkRecord[] }
  observations?: { intents?: WorkRecord[]; coverage?: WorkRecord[] }
  suggestions?: WorkRecord[]
}
