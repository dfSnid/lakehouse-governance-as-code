"""Fakes for the Databricks workspace and Ontos, injected in place of the real clients."""
from __future__ import annotations

import itertools
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from dp_integration.config import Settings
from dp_integration.contract import Version

ROOT = Path(__file__).resolve().parents[1]
# The values the pipeline commands need, so the tests pass whatever environments/dev.yml holds
# (the placeholder template included).
ENV = {"DP_DATABRICKS_HOST": "adb-0.0.azuredatabricks.net", "DP_WAREHOUSE_ID": "wh0",
       "DP_ONTOS_URL": "https://ontos.example"}

CONTRACT = """\
apiVersion: v3.1.0
kind: DataContract
id: sample-product
name: Sample product
version: {version}
status: {status}
domain: sales
dataProduct: sample-product
team:
  name: Sample team
  members:
    - username: owner@example.com
      role: Business Data Owner
servers:
  - server: dev
    type: databricks
    catalog: sample_catalog
    schema: sample_schema
schema:
  - name: title
    physicalName: title
    description: One row per title
    properties:
      - name: title_id
        description: Title key
      - name: title_name
        description: Display name
customProperties:
  - property: governanceTier
    value: 2
  - property: securityClassification
    value: internal
"""

ACCESS = """\
contract: sample-product
grants:
  - principal: readers
    privileges: [SELECT]
    objects: [title]
"""


@pytest.fixture
def settings() -> Settings:
    return Settings.load(ROOT / "config", "dev", overrides=ENV)


@pytest.fixture
def write_contract(tmp_path):
    def write(version="1.0.0", status="active", access=ACCESS, name="sample.odcs.yaml") -> Path:
        path = tmp_path / name
        path.write_text(CONTRACT.format(version=version, status=status), encoding="utf-8")
        if access is not None:
            (tmp_path / name.replace(".odcs.yaml", ".access.yaml")).write_text(textwrap.dedent(access))
        return path
    return write


class FakeTables:
    def __init__(self, tables):
        self.tables = tables

    def get(self, full_name):
        if full_name not in self.tables:
            from databricks.sdk.errors import NotFound
            raise NotFound(f"Table '{full_name}' does not exist.")
        return self.tables[full_name]


class FakeSchemas:
    def __init__(self, schemas):
        self.schemas = schemas

    def get(self, full_name):
        if full_name not in self.schemas:
            from databricks.sdk.errors import NotFound
            raise NotFound(f"Schema '{full_name}' does not exist.")
        return SimpleNamespace(full_name=full_name)


class FakeTagAssignments:
    def __init__(self, tags):
        self.tags = tags  # {full_name: {key: value}}

    def list(self, entity_type, entity_name):
        return [SimpleNamespace(tag_key=k, tag_value=v) for k, v in self.tags.get(entity_name, {}).items()]


class FakeGrants:
    def __init__(self, current):
        self.current = current  # {(securable, name): {principal: {privilege}}}
        self.updates = []

    def get(self, securable, full_name):
        assignments = [SimpleNamespace(principal=p, privileges=[SimpleNamespace(value=v) for v in sorted(privs)])
                       for p, privs in self.current.get((securable, full_name), {}).items()]
        return SimpleNamespace(privilege_assignments=assignments)

    def update(self, securable, full_name, changes):
        self.updates.append((securable, full_name, changes))


class FakeWorkspace:
    def __init__(self, tables=None, grants=None, tags=None, schemas=None):
        self.tables = FakeTables(tables or {})
        # A schema exists when one of its tables does, unless the test lists them.
        self.schemas = FakeSchemas(set(schemas) if schemas is not None else
                                   {name.rsplit(".", 1)[0] for name in (tables or {})})
        self.grants = FakeGrants(grants or {})
        self.entity_tag_assignments = FakeTagAssignments(tags or {})


def table(columns, comment=None, properties=None, table_type="MANAGED"):
    return SimpleNamespace(columns=[SimpleNamespace(name=n, comment=c) for n, c in columns.items()],
                           comment=comment, properties=properties or {}, table_type=table_type)


@pytest.fixture(params=["1.0.2", "1.1.0"])
def ontos_version(request, monkeypatch):
    """Runs a test once against each Ontos version FakeOntos models."""
    monkeypatch.setattr(FakeOntos, "version", request.param)
    return request.param


class FakeOntos:
    """The OntosClient methods the sync and role mapper use, on an in-memory store. It models the
    v1.0.2 API, or v1.1.0 where they differ: the import returns a batch result, a PUT refuses a status
    change, teams have domain_ids, and products get Ontos's own custom properties."""

    base_url = "https://ontos.example"
    version = "1.0.2"

    def __init__(self, fail_update=False):
        self.contracts, self.roles, self.calls = {}, [], []
        self.domains, self.teams, self.products = {}, {}, {}
        self.levels = [{"name": "Bronze", "level_order": 1}, {"name": "Silver", "level_order": 2},
                       {"name": "Gold", "level_order": 3}]
        self.breaking = False  # make a non-draft product PUT fail with 409
        self.fail_update = fail_update
        self.publisher_member_of = None  # a team id: list contracts as Ontos shows them to the publisher
        self.skip_import = False  # v1.1.0: report the import as skipped, with HTTP 200
        self._ids = (f"id-{n}" for n in itertools.count(1))

    def _record(self, **fields):
        record = dict(id=next(self._ids), **fields)
        self.contracts[record["id"]] = record
        return record

    def list_contracts(self, include_history=True):
        def visible(c):  # Ontos v1.0.2: a non-admin sees active, deprecated, or its own team's contracts
            return (self.publisher_member_of is None or c["status"] in ("active", "deprecated")
                    or (c.get("owner_team_id") and c["owner_team_id"] == self.publisher_member_of))
        return [dict(id=c["id"], name=c["name"], dataProduct=c.get("dataProduct"))
                for c in self.contracts.values() if visible(c)]

    def get_contract(self, contract_id):
        return dict(self.contracts[contract_id])

    def import_odcs(self, document):
        self.calls.append(("import", document["version"]))
        if self.version == "1.1.0" and self.skip_import:
            return {"created": 0, "skipped": 1, "failed": 0, "total": 1, "created_ids": [],
                    "items": [{"index": 0, "status": "skipped", "message": "already exists"}]}
        team = next((t["id"] for t in self.teams.values() if t["name"] == document.get("owner")), None)
        record = self._record(name=document["name"], dataProduct=document.get("dataProduct"),
                              version=str(document["version"]), status=document["status"],
                              customProperties={"sourceId": document["id"]}, owner_team_id=team)
        if self.version == "1.0.2":
            return record
        return {"created": 1, "skipped": 0, "failed": 0, "total": 1, "created_ids": [record["id"]],
                "items": [{"index": 0, "status": "created", "entity_id": record["id"], "name": record["name"]}]}

    def _refuse_status_change(self, stored, body):
        if self.version == "1.1.0" and body.get("status") not in (None, stored["status"]):
            from dp_integration.ontos import OntosError
            raise OntosError(f"400 status cannot change in an update ({stored['status']} -> {body['status']})", 400)

    def new_version(self, contract_id, version):
        self.calls.append(("new_version", contract_id, version))
        parent = self.contracts[contract_id]
        return self._record(name=parent["name"], dataProduct=parent.get("dataProduct"), version=version,
                            status="draft", customProperties={}, parent=contract_id)

    def update_contract(self, contract_id, body):
        self.calls.append(("update", contract_id))
        if self.fail_update:
            from dp_integration.ontos import OntosError
            raise OntosError("PUT failed", 500)
        self._refuse_status_change(self.contracts[contract_id], body)
        self.contracts[contract_id].update(body)
        return dict(self.contracts[contract_id])

    def delete_contract(self, contract_id):
        self.calls.append(("delete", contract_id))
        del self.contracts[contract_id]

    def change_status(self, contract_id, status):
        self.calls.append(("change_status", contract_id, status))
        old = self.contracts[contract_id]["status"]
        self.contracts[contract_id]["status"] = status
        return {"from": old, "to": status}

    def add_quality_item(self, contract_id, item):
        self.calls.append(("quality", contract_id, item["dimension"], item["score_percent"]))
        return item

    def list_roles(self):
        return [dict(r) for r in self.roles]

    def create_role(self, role):
        self.roles.append(dict(role, id=f"role-{len(self.roles) + 1}"))

    def update_role(self, role):
        self.calls.append(("update_role", role["name"], role["assigned_groups"]))
        self.roles = [dict(role) if r["id"] == role["id"] else r for r in self.roles]

    # Domains and teams
    def list_domains(self):
        return [dict(d) for d in self.domains.values()]

    def create_domain(self, domain):
        self.calls.append(("create_domain", domain["name"]))
        record = dict(domain, id=next(self._ids))
        self.domains[record["id"]] = record
        return dict(record)

    def update_domain(self, domain_id, domain):
        self.calls.append(("update_domain", domain_id))
        self.domains[domain_id].update(domain)
        return dict(self.domains[domain_id])

    def list_teams(self):
        return [dict(t, members=[dict(m) for m in t["members"]]) for t in self.teams.values()]

    def _team_fields(self, team):  # each version ignores the other's domain fields
        drop = ("domain_ids", "primary_domain_id") if self.version == "1.0.2" else ("domain_id",)
        return {k: v for k, v in team.items() if k not in drop}

    def create_team(self, team):
        self.calls.append(("create_team", team["name"]))
        record = dict(self._team_fields(team), id=next(self._ids), members=[])
        if self.version == "1.1.0":
            record.setdefault("domain_ids", [])
            record.setdefault("primary_domain_id", None)
        self.teams[record["id"]] = record
        return dict(record, members=[])

    def update_team(self, team_id, team):
        self.calls.append(("update_team", team_id))
        self.teams[team_id].update(self._team_fields(team))
        return dict(self.teams[team_id])

    def add_team_member(self, team_id, member):
        self.calls.append(("add_member", team_id, member["member_identifier"]))
        self.teams[team_id]["members"].append(dict(member))
        return dict(member)

    # Data products, stored the way Ontos returns them: v1.0.2 uses snake_case for the ODPS fields,
    # v1.1.0 camelCase. The Ontos fields (owner_team_id, certification_level) are snake_case in both.
    def _product_fields(self, body):
        props, ports, contract = ("custom_properties", "output_ports", "contract_id") if self.version == "1.0.2" \
            else ("customProperties", "outputPorts", "contractId")
        return {"name": body.get("name"), "version": body.get("version"), "domain": body.get("domain"),
                "description": body.get("description"), "owner_team_id": body.get("owner_team_id"),
                props: [dict(p) for p in body.get("customProperties") or []],
                ports: [{"name": p["name"], "version": p["version"], "description": p.get("description"),
                         contract: p.get("contractId")} for p in body.get("outputPorts") or []]}

    def list_products(self, include_history=False):
        products = list(self.products.values())
        if not include_history:  # as Ontos without the parameter: only the newest version of each family
            newest = {}
            for p in products:
                family = p.get("version_family_id") or p["id"]
                if family not in newest or Version(p["version"]) > Version(newest[family]["version"]):
                    newest[family] = p
            products = list(newest.values())
        return [dict(p) for p in products]

    def get_product(self, product_id):
        return dict(self.products[product_id])

    def create_product(self, product):
        self.calls.append(("create_product", product["status"]))
        record = dict(self._product_fields(product), id=next(self._ids), status=product["status"],
                      certification_level=None, publication_scope="none", parent=product.get("parentProductId"))
        record["version_family_id"] = product.get("versionFamilyId") or record["id"]
        if self.version == "1.1.0":
            record["customProperties"].append({"property": "ontosOriginalDomain", "value": f'["{product["domain"]}"]'})
        self.products[record["id"]] = record
        return dict(record)

    def update_product(self, product_id, product):
        self.calls.append(("update_product", product_id, product["status"]))
        record = self.products[product_id]
        if self.breaking and record["status"] != "draft":
            from dp_integration.ontos import OntosError
            raise OntosError("409 Breaking changes detected", 409)
        self._refuse_status_change(record, product)
        record.update(self._product_fields(product), status=product["status"])
        return dict(record)

    def change_product_status(self, product_id, status):
        from dp_integration.lifecycle import DATA_PRODUCT_TRANSITIONS
        self.calls.append(("product_status", product_id, status))
        old = self.products[product_id]["status"]
        assert status in DATA_PRODUCT_TRANSITIONS[old], f"Ontos rejects {old} -> {status}"
        self.products[product_id]["status"] = status
        return {"from": old, "to": status}

    def certify_product(self, product_id, level_order, notes=None):
        self.calls.append(("certify", product_id, level_order))
        assert self.products[product_id]["status"] == "active"
        self.products[product_id]["certification_level"] = level_order
        return dict(self.products[product_id])

    def decertify_product(self, product_id, notes=None):
        self.calls.append(("decertify", product_id))
        self.products[product_id]["certification_level"] = None
        return dict(self.products[product_id])

    def set_publication_scope(self, product_id, scope):
        self.calls.append(("scope", product_id, scope))
        assert scope == "none" or self.products[product_id]["status"] == "active"
        self.products[product_id]["publication_scope"] = scope
        return dict(self.products[product_id])

    def list_certification_levels(self):
        return [dict(lv) for lv in self.levels]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass
