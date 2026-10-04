"""Retail guideline/tool associations; no task-specific data or execution logic."""

from dataclasses import dataclass


POLICY = "data/tau2/domains/retail/policy.md"
TOOLS = "src/tau2/domains/retail/tools.py"


@dataclass(frozen=True)
class Guidance:
    key: str
    condition: str | None
    action: str
    tools: tuple[str, ...]
    source_kind: str
    source: str


GUIDANCE = (
    Guidance(
        "authenticate", "The customer provides email or full name and zip code to authenticate",
        "Authenticate with the applicable lookup tool before giving account information; an already supplied user ID does not replace authentication.",
        ("find_user_id_by_email", "find_user_id_by_name_zip"), "policy_requirement",
        f"{POLICY}#Overview + {TOOLS}#find_user_id_by_email/find_user_id_by_name_zip",
    ),
    Guidance(
        "account_order_lookup", "An authenticated customer asks about their account or orders, including when they do not know an order ID",
        "Use get_user_details for the authenticated account and inspect its orders. Use get_order_details for candidate orders to check status and items, then identify the requested order or product. Do not require an order ID that these tools can discover. Ask the customer to clarify only if the available facts remain ambiguous.",
        ("get_user_details", "get_order_details"), "tool_usage_hint",
        f"{POLICY}#Overview + {TOOLS}#get_user_details/get_order_details",
    ),
    Guidance(
        "product_directory", "The customer asks about a product and its product ID is not yet available from the conversation or tool results",
        "Use list_all_product_types once to identify the product ID. When a valid product ID is already available, use it directly and do not rediscover the directory.",
        ("list_all_product_types",), "tool_usage_hint",
        f"{POLICY}#Domain basic + {TOOLS}#list_all_product_types",
    ),
    Guidance(
        "product_details", "The customer asks about product options, available variants, item facts, or a price difference",
        "Use product or item details and calculate when needed. If asked for a count, count only variants in the returned product whose available field is true; state which product and availability scope the count covers. Do not infer a store-wide count from one product, and do not call the directory again when a valid product ID is already known.",
        ("get_product_details", "get_item_details", "calculate"), "tool_usage_hint",
        f"{POLICY}#Domain basic + {TOOLS}#get_product_details/get_item_details/calculate",
    ),
    Guidance(
        "default_address", "The authenticated customer wants to change their default profile address rather than a particular order's shipping address",
        "Use modify_user_address for the default profile address only, after giving the complete change details and obtaining explicit confirmation. For a pending order's shipping address, use modify_pending_order_address after checking order status and obtaining confirmation.",
        ("modify_user_address",), "policy_requirement",
        f"{POLICY}#Overview/Modify pending order + {TOOLS}#modify_user_address/modify_pending_order_address",
    ),
    Guidance(
        "transfer", "The customer explicitly asks for a human agent or the request cannot be handled within the allowed Retail actions",
        "Follow the public transfer policy: call transfer_to_human_agents first, then give the specified transfer message only after a successful tool result.",
        ("transfer_to_human_agents",), "policy_requirement",
        f"{POLICY}#Overview + {TOOLS}#transfer_to_human_agents",
    ),
    Guidance(
        "completion_evidence", None,
        "Claim a transfer, return, exchange, cancellation, or modification has completed only after its corresponding tool returns success. Before that, state the action is pending. Check returned order, user, product and payment fields for facts; never infer a card's last digits from payment_method_id text. Preserve authentication, eligibility checks, and explicit confirmation before writes.",
        (), "policy_requirement", f"{POLICY}#Overview/Generic action rules + {TOOLS}#write tools",
    ),
)


def coverage(official_tool_names: list[str], policy_associations: dict[str, list[str]]) -> dict:
    associations = {
        name: [f"policy:{heading}" for heading, tools in policy_associations.items() if name in tools]
        + [f"guidance:{item.key}" for item in GUIDANCE if name in item.tools]
        for name in official_tool_names
    }
    return {
        "official_tools": sorted(official_tool_names),
        "associations": associations,
        "missing": sorted(name for name, sources in associations.items() if not sources),
    }
