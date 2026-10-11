"""Versioned template/episode isolation; shared capabilities do not disable checks."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re

DIMENSIONS = (
    "initial_visible_information",
    "ambiguity_missing_structure",
    "user_change_timing",
    "condition_dependencies",
    "confirmation_relationship",
    "terminal_goal",
)
RESERVED = ("H1", "H2", "H3", "H4")


def fingerprint(descriptor):
    if set(descriptor) != set(DIMENSIONS):
        raise ValueError("All causal template dimensions are required; instance metadata excluded")
    return hashlib.sha256(
        json.dumps(descriptor, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def register(groups):
    owners, ids = {}, set()
    for group in groups:
        if group["causal_template_group"] in ids:
            raise ValueError("Duplicate template group name")
        ids.add(group["causal_template_group"])
        fp = fingerprint(group["descriptor"])
        if fp in owners and owners[fp] != group["split"]:
            raise ValueError("Equivalent causal template crosses splits")
        owners[fp] = group["split"]
        if group.get("reserved_holdout_overlap"):
            raise ValueError("Protected holdout structure cannot enter data")
        group["normalized_fingerprint"] = fp
    return groups


def check_episode(episode, groups):
    table = {g["causal_template_group"]: g for g in groups}
    group = table[episode["causal_template_group"]]
    if (
        episode["split"] != group["split"]
        or episode["capability_family"] != group["capability_family"]
    ):
        raise ValueError("Episode crosses registered template split")
    if episode["template_fingerprint"] != group["normalized_fingerprint"]:
        raise ValueError("Unfrozen episode template")
    if episode.get("source") != "independently_authored_public_business_rules":
        raise ValueError("DEV/Oracle/B0 cannot be a data source")
    return True


def normalized_text(text):
    # Only public common system/schema is excluded by caller; scenario text stays.
    text = re.sub(r"(?:ORD|ITEM|PTTRAIN|PTVALID|PTDEV)[A-Za-z0-9_-]+", "<ID>", text)
    text = re.sub(r"\d+(?:\.\d+)?", "<N>", text)
    return re.sub(r"\s+", "", text).lower()


def check_texts(rows):
    owners = {}
    for row in rows:
        for text in row["scenario_texts"]:
            key = normalized_text(text)
            if not key:
                continue
            if key in owners and owners[key] != row["split"]:
                raise ValueError("Normalized scenario/user script duplicates across splits")
            owners[key] = row["split"]
    return True


def new_groups():
    def make(name, split, initial, missing, timing, dependencies, confirmation, terminal):
        return dict(
            causal_template_group=name,
            split=split,
            capability_family="order_return_workflow",
            source="independently_authored_public_business_rules",
            reserved_holdout_overlap=[],
            descriptor=dict(
                zip(DIMENSIONS, (initial, missing, timing, dependencies, confirmation, terminal))
            ),
        )

    return [
        make(
            "TRAIN.preview_only_complete_intent",
            "train",
            ["order_id", "item_id", "quantity", "reason", "explicit_preview_only_permission"],
            "none",
            "no_followup",
            ["read_order", "validate_known_line", "create_preview"],
            "no_user_confirmation_allowed",
            "one_valid_preview_without_request",
        ),
        make(
            "TRAIN.unique_product_name_return",
            "train",
            ["order_id", "unique_product_name_selector", "quantity", "reason"],
            "item_id_resolved_by_visible_tool_product_match",
            "confirm_after_visible_preview",
            [
                "read_order",
                "unique_product_name_match",
                "create_preview",
                "exact_confirm",
                "submit",
            ],
            "latest_visible_preview_matching_named_product",
            "one_confirmed_request",
        ),
        make(
            "TRAIN.latest_delivered_order_return",
            "train",
            [
                "latest_delivered_order_selector",
                "unique_product_name_selector",
                "quantity",
                "reason",
            ],
            "order_and_line_resolved_by_visible_order_listing_and_details",
            "confirm_after_visible_preview",
            [
                "list_orders",
                "unique_latest_delivered",
                "read_order",
                "unique_product_match",
                "create_preview",
                "exact_confirm",
                "submit",
            ],
            "latest_visible_preview_matching_explicit_selection_rule",
            "one_confirmed_request",
        ),
        make(
            "TRAIN.all_available_single_line_return",
            "train",
            ["order_id", "item_id", "all_currently_available_quantity_rule", "reason"],
            "quantity_derived_from_current_visible_line_inventory",
            "confirm_after_visible_preview",
            [
                "read_order",
                "available_quantity_arithmetic",
                "create_preview",
                "exact_confirm",
                "submit",
            ],
            "latest_visible_preview_matching_all_available_rule",
            "one_confirmed_request",
        ),
        make(
            "VALID.preview_reason_clarification",
            "sft_valid",
            ["order_id", "item_id", "quantity", "explicit_preview_only_permission"],
            "reason_unknown_until_actual_clarification",
            "reason_after_agent_question_before_preview",
            ["read_order", "wait_for_reason", "create_preview"],
            "no_user_confirmation_allowed",
            "one_valid_preview_without_request",
        ),
        make(
            "VALID.preview_quantity_after_order_read",
            "sft_valid",
            [
                "order_id",
                "item_id",
                "reason",
                "wait_for_user_quantity_after_order_read",
                "explicit_preview_only_permission",
            ],
            "quantity_unknown_until_actual_order_observation",
            "quantity_after_actual_order_read_before_preview",
            ["read_order", "wait_for_user_quantity", "create_preview"],
            "no_user_confirmation_allowed",
            "one_valid_preview_without_request",
        ),
    ]


def dev_groups(tasks):
    groups = []
    for task in tasks:
        followups = []
        for script in task["user_followups"]:
            followups.append(
                {k: deepcopy(script[k]) for k in ("trigger", "field", "kind") if k in script}
            )
            if "sets" in script:
                followups[-1]["changed_fields"] = sorted(script["sets"])
        descriptor = dict(
            zip(
                DIMENSIONS,
                (
                    sorted(task["visible_goal_fields"]),
                    {
                        "variant": task["variant"],
                        "initial_constraint": task["user_goal"]["constraint"],
                    },
                    followups,
                    {"workflow": task["family"], "goal_kind": task["user_goal"]["kind"]},
                    "exact_visible_human_confirmation"
                    if task["family_id"] != "F3"
                    else "existing_state_read_only",
                    "confirmed_request"
                    if task["family_id"] != "F3"
                    else "verified_constraint_read_no_mutation",
                ),
            )
        )
        groups.append(
            dict(
                causal_template_group="DEV." + task["task_id"],
                split="DEV",
                capability_family=task["family"],
                descriptor=descriptor,
                reserved_holdout_overlap=[],
                source="frozen_dev_audit_only",
            )
        )
    return groups


def freeze(directory, tasks, authorization, old_split):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    groups = register(dev_groups(tasks) + new_groups())
    policy = {
        "version": "split.policy.v2",
        "status": "SPLIT_APPROVED",
        "authorization": authorization,
        "supersedes_rule_only": str(old_split),
        "old_split_sha256": hashlib.sha256(Path(old_split).read_bytes()).hexdigest(),
        "shared_capabilities_allowed": True,
        "isolation_units": ["capability_family", "causal_template_group", "episode_id"],
        "group_ownership": "template/rewrites/variants/replays/all decisions remain in one split",
        "normalization_dimensions": list(DIMENSIONS),
        "cosmetic_changes_create_new_group": False,
        "tool_sequence_alone_defines_group": False,
        "dev_oracle_b0_supervision_allowed": False,
        "holdout_reserved": list(RESERVED),
        "holdout_instantiated": False,
        "legacy_training_or_selection": False,
        "interpretation": "shared business capabilities with causal-template/episode isolation; limited exposed DEV comparison",
        "groups": groups,
    }
    (directory / "SPLIT_POLICY_V2.json").write_text(
        json.dumps(policy, ensure_ascii=False, indent=2) + "\n"
    )
    return policy
