"""Unit tests for the SKU / add-on catalog, dependency rules and update payload builder."""
import pytest

from s1am import licenses
from s1am.cli import _apply_license_changes, _parse_sku_specs


def _b(name, count=-1, surface="Total Agents"):
    return {"name": name, "surfaces": [{"name": surface, "count": count}]}


def _m(*names):
    return [{"name": n} for n in names]


# Trimmed copy of a live account (USEA2) with SKUs/add-ons outside the s1am catalog.
LIVE = {
    "bundles": [
        {"name": "control", "displayName": "Endpoint Security - Control", "majorVersion": 1,
         "surfaces": [{"name": "Total Agents", "count": -1}]},
        {"name": "core", "displayName": "Endpoint Security - Core", "majorVersion": 1,
         "surfaces": [{"name": "Total Agents", "count": 1}]},
        {"name": "singularity_data_lake", "displayName": "Data Ingest", "majorVersion": 1,
         "surfaces": [{"name": "Average GB/Day", "count": 10},
                      {"name": "Long-Range Query Credits", "count": 0}]},
        {"name": "unified_identity", "displayName": "Identity Security", "majorVersion": 1,
         "surfaces": [{"name": "Total Users", "count": 5}]},
    ],
    "modules": [
        {"name": "binary_vault_benign", "displayName": "Binary Vault - Benign Files"},
        {"name": "purple_ai", "displayName": "Purple AI Foundations"},
    ],
    "settings": [
        {"groupName": "ranger_ad_mode", "setting": "Full", "settingGroup": "ranger_ad_mode"},
        {"groupName": "dv_retention", "setting": "14 Days", "settingGroup": "dv_retention"},
    ],
}


# ── catalog ──────────────────────────────────────────────────────────────────

def test_catalog_counts():
    assert len(licenses.SKUS) == 7
    assert len(licenses.ADDONS) == 17


def test_resolve_by_internal_or_display_name():
    assert licenses.resolve_sku("Identity Security")["name"] == "unified_identity"
    assert licenses.resolve_sku("HYPERAUTOMATION")["surface"] == "Action Packs"
    assert licenses.resolve_addon("Wayfinder MDR Essentials")["name"] == "wayfinder_mdr_essentials"
    assert licenses.resolve_addon("purple_ai")["displayName"] == "Purple AI Foundations"


def test_hidden_items_not_in_catalog():
    assert licenses.resolve_sku("core") is None
    assert licenses.resolve_addon("binary_vault_benign") is None
    assert licenses.resolve_addon("Wayfinder MDR Elite") is None


# ── validate_licenses ────────────────────────────────────────────────────────

@pytest.mark.parametrize("base", ["complete", "control"])
def test_identity_with_complete_or_control_ok(base):
    assert licenses.validate_licenses([_b(base), _b("unified_identity", 5, "Total Users")], []) == []


def test_identity_without_complete_or_control_fails():
    errs = licenses.validate_licenses([_b("core"), _b("unified_identity", 5, "Total Users")], [])
    assert len(errs) == 1 and "Identity Security" in errs[0]


def test_wayfinder_essentials_needs_both_dependencies():
    assert licenses.validate_licenses(
        [], _m("wayfinder_mdr_essentials", "purple_ai", "wayfinder_threat_hunting")) == []
    errs = licenses.validate_licenses([], _m("wayfinder_mdr_essentials", "purple_ai"))
    assert errs == ["Wayfinder MDR Essentials requires: Wayfinder Threat Hunting"]


def test_dependencies_alone_are_fine():
    assert licenses.validate_licenses([], _m("purple_ai", "wayfinder_threat_hunting")) == []


def test_data_ingest_cannot_be_unlimited():
    errs = licenses.validate_licenses([_b("singularity_data_lake", -1, "Average GB/Day")], [])
    assert len(errs) == 1 and "Data Ingest" in errs[0]


# ── _parse_sku_specs ─────────────────────────────────────────────────────────

def test_parse_sku_specs():
    assert _parse_sku_specs("unified_identity=500, hyperautomation=unlimited,complete") == [
        ("unified_identity", 500), ("hyperautomation", -1), ("complete", None)]
    assert _parse_sku_specs("Identity Security=3") == [("Identity Security", 3)]


def test_parse_sku_specs_invalid():
    with pytest.raises(ValueError):
        _parse_sku_specs("complete=lots")


# ── _apply_license_changes ───────────────────────────────────────────────────

def test_no_changes_preserves_everything():
    lic, changes, errors = _apply_license_changes(LIVE)
    assert errors == []
    assert [b["name"] for b in lic["bundles"]] == ["control", "core", "singularity_data_lake", "unified_identity"]
    assert lic["bundles"][2]["surfaces"][1] == {"name": "Long-Range Query Credits", "count": 0}
    assert lic["bundles"][0]["majorVersion"] == 1
    assert {m["name"] for m in lic["modules"]} == {"binary_vault_benign", "purple_ai"}
    assert lic["settings"] == [{"groupName": "ranger_ad_mode", "setting": "Full"},
                               {"groupName": "dv_retention", "setting": "14 Days"}]


def test_add_identity_and_hyperautomation():
    no_identity = dict(LIVE, bundles=[b for b in LIVE["bundles"] if b["name"] != "unified_identity"])
    lic, changes, errors = _apply_license_changes(
        no_identity, add_skus="Identity Security=250,hyperautomation=unlimited")
    assert errors == []
    by_name = {b["name"]: b for b in lic["bundles"]}
    assert by_name["unified_identity"]["surfaces"] == [{"name": "Total Users", "count": 250}]
    assert by_name["hyperautomation"]["surfaces"] == [{"name": "Action Packs", "count": -1}]
    assert changes["skus_added"] == ["unified_identity=250", "hyperautomation=-1"]


def test_update_existing_count_keeps_other_surfaces():
    lic, changes, errors = _apply_license_changes(LIVE, add_skus="singularity_data_lake=25")
    assert errors == []
    di = next(b for b in lic["bundles"] if b["name"] == "singularity_data_lake")
    assert di["surfaces"] == [{"name": "Average GB/Day", "count": 25},
                              {"name": "Long-Range Query Credits", "count": 0}]
    assert changes["skus_updated"] == ["singularity_data_lake=25"]


def test_new_sku_requires_count():
    _, _, errors = _apply_license_changes(LIVE, add_skus="hyperautomation")
    assert errors and "needs a count" in errors[0]


def test_unknown_sku_and_addon_rejected():
    _, _, errors = _apply_license_changes(LIVE, add_skus="core=5", add_modules="Wayfinder MDR Elite")
    assert len(errors) == 2


def test_removing_control_blocks_identity():
    _, _, errors = _apply_license_changes(LIVE, remove_skus="control")
    assert errors and "Identity Security" in errors[0]


def test_wayfinder_essentials_blocked_until_dependencies_on():
    _, _, errors = _apply_license_changes(LIVE, add_modules="wayfinder_mdr_essentials")
    assert errors == ["Wayfinder MDR Essentials requires: Wayfinder Threat Hunting"]
    lic, changes, errors = _apply_license_changes(
        LIVE, add_modules="Wayfinder Threat Hunting, Wayfinder MDR Essentials")
    assert errors == []
    assert changes["modules_added"] == ["wayfinder_threat_hunting", "wayfinder_mdr_essentials"]


def test_remove_module_case_insensitive():
    lic, changes, errors = _apply_license_changes(LIVE, remove_modules="PURPLE_AI")
    assert errors == []
    assert [m["name"] for m in lic["modules"]] == ["binary_vault_benign"]
    assert changes["modules_removed"] == ["purple_ai"]


def test_dv_retention_updates_only_that_setting():
    lic, changes, _ = _apply_license_changes(LIVE, dv_retention="180 Days")
    assert lic["settings"] == [{"groupName": "ranger_ad_mode", "setting": "Full"},
                               {"groupName": "dv_retention", "setting": "180 Days"}]
    assert changes["settings_changed"] == ["dv_retention=180 Days"]


def test_dv_retention_added_when_missing():
    lic, _, _ = _apply_license_changes(dict(LIVE, settings=[]), dv_retention="30 Days")
    assert lic["settings"] == [{"groupName": "dv_retention", "setting": "30 Days"},
                               {"groupName": "ranger_ad_mode", "setting": "Full"}]


# ── Identity Security Posture Mode follows Identity Security ─────────────────

def test_prune_settings_drops_posture_mode_without_identity():
    settings = [{"groupName": "ranger_ad_mode", "setting": "Full"},
                {"groupName": "dv_retention", "setting": "90 Days"}]
    assert licenses.prune_settings([_b("complete")], settings) == [settings[1]]
    assert licenses.prune_settings([_b("complete"), _b("unified_identity", 1, "Total Users")],
                                   settings) == settings


def test_removing_identity_drops_posture_mode():
    lic, changes, errors = _apply_license_changes(LIVE, remove_skus="Identity Security")
    assert errors == []
    assert lic["settings"] == [{"groupName": "dv_retention", "setting": "14 Days"}]
    assert changes["settings_removed"] == ["ranger_ad_mode"]


def test_adding_identity_adds_default_posture_mode():
    base = dict(LIVE, bundles=[b for b in LIVE["bundles"] if b["name"] != "unified_identity"],
                settings=[{"groupName": "dv_retention", "setting": "14 Days"}])
    lic, changes, errors = _apply_license_changes(base, add_skus="unified_identity=10")
    assert errors == []
    assert {"groupName": "ranger_ad_mode", "setting": "Full"} in lic["settings"]
    assert changes["settings_added"] == ["ranger_ad_mode=Full"]


def test_existing_posture_mode_not_overwritten():
    settings = [{"groupName": "ranger_ad_mode", "setting": "Other"}]
    assert licenses.add_default_settings([_b("unified_identity")], settings) == settings
    assert licenses.add_default_settings([_b("control")], []) == []


# ── Complete SKU: DV retention + default add-ons ─────────────────────────────

def test_complete_defaults_dv_retention_to_14_days():
    assert licenses.add_default_settings([_b("complete")], []) == [
        {"groupName": "dv_retention", "setting": "14 Days"}]
    kept = [{"groupName": "dv_retention", "setting": "90 Days"}]
    assert licenses.add_default_settings([_b("complete")], kept) == kept


def test_existing_dv_retention_kept_without_complete():
    # Live Control accounts carry dv_retention; it must survive a save, only *adding* needs Complete.
    settings = [{"groupName": "dv_retention", "setting": "14 Days"}]
    assert licenses.prune_settings([_b("control")], settings) == settings
    assert licenses.add_default_settings([_b("control")], []) == []


def test_add_default_modules_for_complete():
    out = licenses.add_default_modules(["complete"], [{"name": "purple_ai"}])
    assert [m["name"] for m in out] == ["purple_ai", "rogues"]
    assert licenses.add_default_modules(["control"], []) == []


def test_cli_adding_complete_adds_default_modules_and_dv():
    from s1am.cli import _apply_license_changes
    lic, changes, errors = _apply_license_changes({"bundles": [], "modules": []}, add_skus="complete=unlimited")
    assert errors == []
    assert sorted(m["name"] for m in lic["modules"]) == ["purple_ai", "rogues"]
    assert sorted(changes["modules_added"]) == ["purple_ai", "rogues"]
    assert {"groupName": "dv_retention", "setting": "14 Days"} in lic["settings"]
    # Explicit removal in the same call wins, and existing Complete accounts are untouched.
    lic, changes, _ = _apply_license_changes({"bundles": [], "modules": []},
                                             add_skus="complete=unlimited", remove_modules="rogues")
    assert [m["name"] for m in lic["modules"]] == ["purple_ai"]
    lic, changes, _ = _apply_license_changes({"bundles": [_b("complete")], "modules": []}, add_skus="complete=5")
    assert lic["modules"] == [] and changes["modules_added"] == []
