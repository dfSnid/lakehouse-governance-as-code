import pytest

from dp_integration.contract import ContractError
from dp_integration.domains import DomainRegistry, OntosDomainSync

from conftest import FakeOntos

pytestmark = pytest.mark.usefixtures("ontos_version")

REGISTRY = """\
domains:
  - name: sales
    description: What the business makes and buys
    team:
      name: sales-team
      title: Sales
      groups: [dp-sales-stewards, dp-sales-producers, dp-publishers]
  - name: online
    description: Online orders
    parent: sales
"""


@pytest.fixture
def registry(tmp_path):
    path = tmp_path / "domains.yaml"
    path.write_text(REGISTRY)
    return DomainRegistry.from_file(path)


def test_registry_orders_parents_first(tmp_path):
    path = tmp_path / "domains.yaml"
    path.write_text("domains:\n  - {name: online, parent: sales}\n  - {name: sales}\n")
    assert [d.name for d in DomainRegistry.from_file(path).ordered()] == ["sales", "online"]


@pytest.mark.parametrize("text,message", [
    ("domains:\n  - {name: a, parent: b}\n  - {name: b, parent: a}\n", "cycle"),
    ("domains:\n  - {name: a, parent: nope}\n", "unknown parent"),
    ("domains:\n  - {name: a}\n  - {name: a}\n", "twice"),
    ("domains:\n  - {description: x}\n", "needs a name"),
])
def test_registry_rejects(tmp_path, text, message):
    path = tmp_path / "domains.yaml"
    path.write_text(text)
    with pytest.raises(ContractError, match=message):
        DomainRegistry.from_file(path)


def test_sync_creates_domains_and_team(registry, ontos_version):
    ontos = FakeOntos()
    ids = OntosDomainSync(ontos).sync(registry)
    assert ontos.domains[ids["online"]]["parent_id"] == ids["sales"]
    (team,) = ontos.teams.values()
    if ontos_version == "1.0.2":
        assert team["domain_id"] == ids["sales"]
    else:
        assert team["domain_ids"] == [ids["sales"]] and team["primary_domain_id"] == ids["sales"]
    assert [m["member_identifier"] for m in team["members"]] == [
        "dp-sales-stewards", "dp-sales-producers", "dp-publishers"]


def test_sync_is_idempotent_and_add_only(registry):
    ontos = FakeOntos()
    OntosDomainSync(ontos).sync(registry)
    team = next(iter(ontos.teams.values()))
    team["members"].append({"member_type": "group", "member_identifier": "added-in-ontos"})
    ontos.calls.clear()
    OntosDomainSync(ontos).sync(registry)
    assert ontos.calls == []
    assert len(team["members"]) == 4  # nothing removed


def test_sync_adopts_an_auto_created_domain(registry):
    ontos = FakeOntos()
    existing = ontos.create_domain({"name": "sales", "description": None, "parent_id": None})
    ontos.calls.clear()
    ids = OntosDomainSync(ontos).sync(registry)
    assert ids["sales"] == existing["id"]
    assert ("update_domain", existing["id"]) in ontos.calls
    assert ontos.domains[existing["id"]]["description"] == "What the business makes and buys"


def test_existing_team_gets_the_domain(registry, ontos_version):
    ontos = FakeOntos()
    other = ontos.create_domain({"name": "other", "description": None, "parent_id": None})["id"]
    team = ontos.create_team({"name": "sales-team", "title": "Sales", "domain_id": other,
                              "domain_ids": [other], "primary_domain_id": other})
    ids = OntosDomainSync(ontos).sync(registry)
    stored = ontos.teams[team["id"]]
    if ontos_version == "1.0.2":  # one domain per team: it moves
        assert stored["domain_id"] == ids["sales"]
    else:  # it keeps its other domains and its primary one
        assert stored["domain_ids"] == [other, ids["sales"]] and stored["primary_domain_id"] == other
    assert stored["title"] == "Sales"
