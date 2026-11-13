"""ROAM-001 (FR-036): the companion is still unless the person turned roaming on."""
import pytest

pytest.importorskip("mongomock_motor")

from app.routers import agent as agent_routes  # noqa: E402
from app.routers import users as users_routes  # noqa: E402
from tests._fake_world import fake_world  # noqa: E402


@pytest.fixture
def world():
    with fake_world() as w:
        yield w


def test_the_response_models_default_to_still():
    assert agent_routes.AgentStateResponse.model_fields["agent_roaming_enabled"].default is False
    assert users_routes.GeneralPreferencesResponse.model_fields["agent_roaming_enabled"].default is False


@pytest.mark.parametrize("stored, expected", [(None, False), (False, False), (True, True)])
def test_general_preferences_read_a_missing_value_as_still_and_keep_a_chosen_one(world, stored, expected):
    preferences = {} if stored is None else {"agent": {"roaming_enabled": stored}}
    uid = world.new_user("member", preferences=preferences)
    world.act_as(uid)
    response = world.http.get("/users/preferences/general")
    assert response.status_code == 200, response.text
    assert response.json()["agent_roaming_enabled"] is expected
