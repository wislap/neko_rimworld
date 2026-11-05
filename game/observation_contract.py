"""Native bounded observation registration control contract.

Coverage is accepted only from native acknowledgements, never from a request echo.
The supported condition kinds themselves are advertised by the native catalog.
"""

from .write_contract import _bad, _fields, _text

OBSERVATION_VERSION = "colony_observation.v1"


def validate_observation_request(args):
    _fields(args, ("source", "observations"))
    if args["source"] != "planner" or not isinstance(args["observations"], list) or len(args["observations"]) > 16:
        _bad()
    groups, targets = set(), 0
    for row in args["observations"]:
        _fields(row, ("observation_id", "revision", "conditions"))
        _text(row["observation_id"], 128)
        if row["observation_id"] in groups or type(row["revision"]) is not int or row["revision"] < 1:
            _bad()
        groups.add(row["observation_id"])
        conditions = row["conditions"]
        if not isinstance(conditions, list) or not 1 <= len(conditions) <= 8:
            _bad()
        seen = set()
        for condition in conditions:
            required = {"id", "kind", "target_ids", "max_samples", "expires_at_tick"}
            if not isinstance(condition, dict) or not required <= condition.keys() or condition.keys() - required - {"map_id", "plan_id", "sample_interval_ticks"}:
                _bad()
            _text(condition["id"], 128)
            _text(condition["kind"], 128)
            if condition["id"] in seen:
                _bad()
            seen.add(condition["id"])
            if "map_id" in condition:
                _text(condition["map_id"])
            if "plan_id" in condition:
                _text(condition["plan_id"], 128)
            identifiers = condition["target_ids"]
            if not isinstance(identifiers, list) or not identifiers or len(identifiers) != len(set(identifiers)):
                _bad()
            for identifier in identifiers:
                _text(identifier)
            targets += len(identifiers)
            for field, minimum, maximum in (("max_samples", 1, 1000), ("expires_at_tick", 1, 2**31 - 1), ("sample_interval_ticks", 1, 60000)):
                if field in condition and (type(condition[field]) is not int or not minimum <= condition[field] <= maximum):
                    _bad()
    if targets > 64:
        _bad()


def validate_observation_result(data):
    _fields(data, ("source", "observations", "registrations"))
    validate_observation_request({"source": data["source"], "observations": data["observations"]})
    registrations = data["registrations"]
    expected = {row["observation_id"]: row for row in data["observations"]}
    if not isinstance(registrations, list) or len(registrations) != len(expected):
        _bad()
    seen = set()
    for registration in registrations:
        required = {"observation_id", "revision", "status", "covered_condition_ids", "receipt_id"}
        if not isinstance(registration, dict) or not required <= registration.keys() or registration.keys() - required - {"reason"}:
            _bad()
        identifier = registration["observation_id"]
        if not isinstance(identifier, str) or identifier not in expected or identifier in seen:
            _bad()
        seen.add(identifier)
        if registration["revision"] != expected[identifier]["revision"] or type(registration["revision"]) is not int:
            _bad()
        if registration["status"] not in {"registered", "rejected"}:
            _bad()
        _text(registration["receipt_id"])
        covered = registration["covered_condition_ids"]
        allowed = {row["id"] for row in expected[identifier]["conditions"]}
        if not isinstance(covered, list) or any(not isinstance(item, str) or item not in allowed for item in covered) or len(covered) != len(set(covered)):
            _bad()
        if registration["status"] == "rejected" and covered:
            _bad()
        if "reason" in registration:
            _text(registration["reason"], 2048)
