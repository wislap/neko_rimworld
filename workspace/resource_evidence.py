"""Selected native source buckets, without ownership or production inferences."""

from copy import deepcopy


def snapshot(rows, scope, *, observation_id, load_epoch, game_tick, sources, definitions=None):
    if (not isinstance(sources, list) or not sources or len(sources) != len(set(sources))
            or any(source not in {"spawned_ground", "player_pawn_held", "other_held", "trader_stock"} for source in sources)):
        raise ValueError("Explicitly select the observed resource source categories")
    quantities = {}
    for row in rows:
        if row.get("coverage") != "complete_in_scope" or row.get("status") in {"unknown", "partial"}:
            continue
        quantities[row["def_name"]] = sum(bucket["units"] for bucket in row["sources"] if bucket["source"] in sources)
    selected = sorted(definitions if definitions is not None else quantities)
    effective_scope = {key: deepcopy(value) for key, value in scope.items() if key != "selection"}
    effective_scope.update(definitions=selected, selected_sources=sorted(sources),
                           meaning="Observed source quantities; ownership and usability require separate evidence")
    return {"observation_id": observation_id, "load_epoch": load_epoch, "game_tick": game_tick,
            "map_id": scope["map_id"], "scope": effective_scope, "native_scope": deepcopy(scope),
            "quantities": quantities, "complete": False}


def observed_snapshot(data, decision, *, observation_id, load_epoch, game_tick, target_ids):
    """Update only a fully selected definition scope, keeping unknown rows unknown."""
    scope = decision["snapshot"]["scope"]
    definitions = scope.get("definitions", [])
    if (data.get("map_id") != decision["snapshot"]["map_id"] or not definitions
            or not set(definitions) <= set(target_ids) or not scope.get("selected_sources")):
        return None
    rows = [row for row in data.get("objects", []) if row.get("definition", row.get("def_name")) in definitions]
    known_scopes = [row["scope"] for row in rows if isinstance(row.get("scope"), dict)]
    if not known_scopes:
        base_scope = {key: value for key, value in scope.items()
                      if key not in {"definitions", "selected_sources", "meaning"}}
    else:
        base_scope = known_scopes[0]
        for current in known_scopes[1:]:
            if {k: v for k, v in current.items() if k != "selection"} != {k: v for k, v in base_scope.items() if k != "selection"}:
                raise ValueError("Native observed resource sources have inconsistent scope")
    return snapshot(rows, base_scope, observation_id=observation_id, load_epoch=load_epoch, game_tick=game_tick,
                    sources=scope["selected_sources"], definitions=definitions)
