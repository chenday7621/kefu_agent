"""V0 tool associations completed using only public Retail policy/tool definitions."""

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
        "condition": "An authenticated customer asks about their profile or order details",
        "action": "Use the applicable official Retail lookup tool to get current account or order facts.",
        "tools": ("get_user_details", "get_order_details"),
        "source": "policy.md#Retail agent policy; tools.py#get_user_details/get_order_details",
    },
    {
        "key": "product_lookup",
        "condition": "The customer asks about product options, availability, or price difference",
        "action": "Use official Retail product and calculation tools for facts; do not invent availability or prices.",
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
