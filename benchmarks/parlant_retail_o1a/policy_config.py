"""O1-A associations using the complete official Retail policy and tool definitions."""

ASSOCIATIONS = (
    {
        "key": "identity_lookup",
        "condition": "The customer provides their email address or full name and zip code for identity verification",
        "action": "Authenticate with the applicable official Retail lookup tool before providing account information.",
        "tools": ("find_user_id_by_email", "find_user_id_by_name_zip"),
        "source": "policy.md#Retail agent policy; tools.py#find_user_id_by_email/find_user_id_by_name_zip",
    },
    {
        "key": "account_lookup",
        "condition": "An authenticated customer asks about their account, an order, or an item in an order, whether or not they know its order ID",
        "action": "Use get_user_details to obtain account facts and the customer's order IDs when needed. If a valid target order ID is already known, use get_order_details directly. Otherwise use the order IDs returned by get_user_details to check plausible orders with get_order_details and match the customer's description against actual order and item facts; do not require the customer to supply an ID that a tool has already provided. Do not assume the order list is sorted by date or guess which order is most recent from its position. If multiple matching orders remain and tool facts cannot distinguish them, ask the customer to clarify. IDs from official tool results may be passed to later official tools. Continue to follow all official identity verification and confirmation requirements.",
        "tools": ("get_user_details", "get_order_details"),
        "source": "policy.md#Retail agent policy; tools.py#get_user_details/get_order_details",
    },
    {
        "key": "product_lookup",
        "condition": "The customer asks about product options, availability, or price difference",
        "action": "Use official Retail product and calculation tools for facts; do not invent availability or prices. When a valid product ID is already available from the customer or an official tool result, use get_product_details directly as needed. Use list_all_product_types to discover an unknown product ID, and reuse an available directory result unless new information makes another lookup necessary.",
        "tools": ("get_product_details", "get_item_details", "list_all_product_types", "calculate"),
        "source": "policy.md#Product; tools.py#product lookup/calculation",
    },
    {
        "key": "default_address",
        "condition": "An authenticated customer wants to modify their default user address",
        "action": "Follow the official Retail policy for modifying the customer's default user address.",
        "tools": ("modify_user_address",),
        "source": "policy.md#Retail agent policy; tools.py#modify_user_address",
    },
    {
        "key": "human_transfer",
        "condition": "The customer's request cannot be handled within the allowed Retail actions",
        "action": "Follow the official transfer policy before responding.",
        "tools": ("transfer_to_human_agents",),
        "source": "policy.md#Retail agent policy; tools.py#transfer_to_human_agents",
    },
)


def coverage(official_tool_names: list[str], section_tools: dict[str, list[str]]) -> dict:
    associations = {
        name: [f"policy:{heading}" for heading, tools in section_tools.items() if name in tools]
        + [f"association:{item['key']}" for item in ASSOCIATIONS if name in item["tools"]]
        for name in official_tool_names
    }
    return {
        "official_tools": sorted(official_tool_names),
        "associations": associations,
        "missing": sorted(name for name, sources in associations.items() if not sources),
    }
