from zero.swarm.tools import SWARM_TOOL_NAMES, swarm_catalog


def test_swarm_tools_are_control_plane_only_and_identity_is_not_model_supplied():
    catalog = swarm_catalog()
    assert {tool["name"] for tool in catalog} == SWARM_TOOL_NAMES
    assert "swarm_send" in SWARM_TOOL_NAMES
    assert "swarm_publish_evidence" in SWARM_TOOL_NAMES
    assert "swarm_finish_bootstrap" in SWARM_TOOL_NAMES
    assert "swarm_finish_cycle" in SWARM_TOOL_NAMES
    create_work = next(tool for tool in catalog if tool["name"] == "swarm_create_work")
    assert "claim" in create_work["schema"]["properties"]
    for tool in catalog:
        properties = tool["schema"].get("properties", {})
        assert "worker_id" not in properties
        assert "actor_id" not in properties
