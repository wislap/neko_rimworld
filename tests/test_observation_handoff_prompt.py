"""Observation guidance is conditional and never expands pause authority."""
import hashlib

from ..agent.runtime.prompt import build_system_prompt


def test_observation_handoff_is_conditional_and_owner_guarded():
    front = build_system_prompt(persistent_work=True, autoplay=True)
    planner = build_system_prompt(role='planner', planner_loop=True)
    for prompt in (front, planner):
        assert 'user explicitly permits continued observation' in prompt
        assert 'unresolved fact and reason' in prompt
        assert 'do not claim the action happened' in prompt
        assert 'user, unknown or policy pause' in prompt
        assert 'force pause' in prompt
    assert 'ask the planner through message_planner' in front
    assert 'you cannot resume it yourself' in front
    assert 'verify the current owner and authorization' in planner
    assert 'guarded pause tool only if supported and permitted' in planner
    assert 'leave it paused' in planner


def test_default_read_only_prefixes_remain_byte_identical():
    assert hashlib.sha256(build_system_prompt().encode()).hexdigest() == '7e59b28cc9d455607915604e90cef3252388c0b00d5b69091c76fa2cb3a33a78'
    assert hashlib.sha256(build_system_prompt(persistent_work=True).encode()).hexdigest() == '452e644fdc11234a1fc17b2cf00a275492f9090b233ef13ef44230bf2ed53a32'
