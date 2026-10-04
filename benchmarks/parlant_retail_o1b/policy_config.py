"""O1-B associations using the complete official Retail policy and tool definitions."""

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
        "condition": "The customer asks about product options, availability, price difference, or an item change with required attributes and preferences",
        "action": "Use official Retail product and calculation tools for facts; do not invent availability, attributes, or prices. When a valid product ID is already available from the customer or an official tool result, use get_product_details directly as needed. Use list_all_product_types to discover an unknown product ID, and reuse an available directory result unless new information makes another lookup necessary. Identify which requested attributes are mandatory and which are preferences, including the customer's stated preference order. Check the attributes and availability of all relevant variants returned by the product tool before concluding that none match. Consider only available variants satisfying every mandatory requirement; among those, follow the customer's preference order using tool facts. Do not abandon the main requested change merely because a preference cannot be met. If no available variant meets all mandatory requirements, state the observed differences and ask whether the customer wants to relax a requirement; do not describe a nonmatching item as a match. Apply any explicit change of requirements in the conversation. Follow the official same-product and confirmation rules for changes.",
        "tools": ("get_product_details", "get_item_details", "list_all_product_types", "calculate"),
        "source": "policy.md#Product/Generic action rules/Modify items/Exchange delivered order; tools.py#get_product_details/get_item_details/list_all_product_types/calculate",
    },
    {
        "key": "operation_target_scope",
        "condition": "The customer refers to an order or item to change and also mentions a different order or product as a reference, or the set of items to change is unclear",
        "action": "Use order and item facts to identify the order and original item actually being changed separately from any reference order, reference item, or desired new variant. For a modify, return, or exchange action, use the IDs of the operative order and its original items; a reference order's item ID is not an item in the operative order. Match the desired variant using the relevant product's actual item ID and options. Do not infer that all items in a broad category should be returned or exchanged when the customer's intended item set is ambiguous; clarify the scope before a database update. If the customer explicitly changes the intended scope or requirements later, use the updated request. Preserve official eligibility checks and explicit confirmation of the full action details before any database update.",
        "tools": ("get_order_details", "get_item_details", "get_product_details"),
        "source": "policy.md#Product/Order/Generic action rules/Modify items/Return delivered order/Exchange delivered order; tools.py#get_order_details/get_item_details/get_product_details",
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
