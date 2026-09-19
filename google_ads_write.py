"""
Write / mutate tools for the Google Ads MCP server.

This module is kept SEPARATE from google_ads_server.py on purpose:
 - All state-changing (mutate) operations live in one place, easy to audit.
 - The diff against the upstream (cohnen/mcp-google-ads) repo stays tiny:
   google_ads_server.py only gains a single `import google_ads_write` line.

Safety model
------------
Every write tool defaults to a DRY RUN. Internally that maps to the Google Ads
API `validateOnly=true` flag: the request is fully validated by Google but NO
change is applied. The change is only committed when the caller passes
`confirm=True`. This makes it impossible to alter live campaigns or ad spend by
accident — a second, explicit call is always required.

IMPORTANT — why parameters use `Annotated[..., Field(...)] = default`:
    We deliberately do NOT write `confirm: bool = Field(default=False)`. When a
    function written that way is called DIRECTLY (not through the MCP framework),
    Python's default value is the Field object itself, which is truthy — so
    `confirm` would evaluate True and a "dry run" would actually apply. Using the
    Annotated form keeps a REAL Python default (`False`), so the safety gate
    holds on every call path: direct calls, tests, and MCP tool calls alike.

Access level
------------
These operations work with a Cloud project at "Explorer" access (up to 2,880
operations/day against production accounts). Creating brand-new accounts is NOT
included here and is not permitted on Explorer access.
"""

from typing import List, Dict, Any, Annotated
from pydantic import Field
import requests
import logging

# Import the shared server objects. This is safe because google_ads_server.py
# performs `import google_ads_write` at the very bottom of the file, after all
# of these names are already defined.
from google_ads_server import (
    mcp,
    get_credentials,
    get_headers,
    format_customer_id,
    API_VERSION,
)

logger = logging.getLogger("google_ads_write")

VALID_STATUSES = {"ENABLED", "PAUSED", "REMOVED"}


def _run_mutate(
    resource: str,
    customer_id: str,
    operations: List[Dict[str, Any]],
    confirm: bool,
) -> str:
    """
    Shared helper for all mutate calls.

    Args:
        resource: the REST resource collection, e.g. "campaigns",
                  "campaignBudgets", "adGroups", "adGroupAds", "adGroupCriteria".
        customer_id: target account (any format; normalised here).
        operations: list of Google Ads mutate operation dicts.
        confirm: False => validateOnly dry run; True => actually apply.

    Returns:
        A human-readable summary string.
    """
    # Safety belt: coerce confirm to a real bool. If anything other than a
    # literal True slips through (e.g. a stray Field object), treat it as a dry
    # run rather than risk an unintended write.
    confirm = confirm is True

    try:
        creds = get_credentials()
        headers = get_headers(creds)
    except Exception as e:
        return f"Auth error: {e}"

    formatted_customer_id = format_customer_id(customer_id)
    url = (
        f"https://googleads.googleapis.com/{API_VERSION}"
        f"/customers/{formatted_customer_id}/{resource}:mutate"
    )

    validate_only = not confirm
    payload = {
        "operations": operations,
        "validateOnly": validate_only,
        # We never want a partial apply hiding an error; fail the whole batch.
        "partialFailure": False,
    }

    try:
        response = requests.post(url, headers=headers, json=payload)
    except Exception as e:
        return f"Network error calling {resource}:mutate: {e}"

    if response.status_code != 200:
        mode = "validation (dry run)" if validate_only else "apply"
        return (
            f"Error during {mode} on {resource} for account "
            f"{formatted_customer_id}:\n{response.text}"
        )

    if validate_only:
        return (
            f"DRY RUN OK ✅ — {len(operations)} operation(s) on {resource} "
            f"for account {formatted_customer_id} are valid and would be applied.\n"
            f"Nothing was changed. Re-run the same call with confirm=true to apply."
        )

    results = response.json().get("results", [])
    changed = [r.get("resourceName", "?") for r in results]
    lines = [
        f"APPLIED ✅ — {len(changed)} change(s) committed on {resource} "
        f"for account {formatted_customer_id}:"
    ]
    lines.extend(f"  - {rn}" for rn in changed)
    return "\n".join(lines)


def _normalise_status(status: str) -> str:
    s = str(status).strip().upper()
    if s not in VALID_STATUSES:
        raise ValueError(
            f"Invalid status '{status}'. Must be one of: {', '.join(sorted(VALID_STATUSES))}"
        )
    return s


# Reusable annotated types ----------------------------------------------------
CustomerId = Annotated[str, Field(description="Google Ads customer ID (10 digits, dashes ok).")]
StatusArg = Annotated[str, Field(description="New status: ENABLED, PAUSED, or REMOVED.")]
ConfirmArg = Annotated[
    bool,
    Field(
        description="Must be true to actually apply the change. When false (the "
        "default), runs a dry run that only validates the change without applying it."
    ),
]


# ---------------------------------------------------------------------------
# Campaign status
# ---------------------------------------------------------------------------
@mcp.tool()
async def set_campaign_status(
    customer_id: CustomerId,
    campaign_id: Annotated[str, Field(description="Numeric campaign ID to update.")],
    status: StatusArg,
    confirm: ConfirmArg = False,
) -> str:
    """
    Enable, pause, or remove a campaign.

    DEFAULTS TO A DRY RUN. Call once to validate (confirm=false), then call again
    with confirm=true to apply. REMOVED is effectively permanent — a removed
    campaign cannot be re-enabled (create a new one instead).
    """
    try:
        status = _normalise_status(status)
    except ValueError as e:
        return str(e)

    cid = format_customer_id(customer_id)
    op = {
        "updateMask": "status",
        "update": {
            "resourceName": f"customers/{cid}/campaigns/{campaign_id}",
            "status": status,
        },
    }
    return _run_mutate("campaigns", customer_id, [op], confirm)


# ---------------------------------------------------------------------------
# Campaign budget
# ---------------------------------------------------------------------------
@mcp.tool()
async def update_campaign_budget(
    customer_id: CustomerId,
    budget_id: Annotated[
        str,
        Field(
            description="Numeric campaign BUDGET id (campaign_budget.id from GAQL, "
            "NOT the campaign id). Query it with: SELECT campaign.name, "
            "campaign_budget.id, campaign_budget.amount_micros FROM campaign"
        ),
    ],
    amount: Annotated[
        float,
        Field(
            description="New daily budget in the account's currency units (e.g. 50 "
            "for $50.00/day). Converted to micros internally."
        ),
    ],
    confirm: ConfirmArg = False,
) -> str:
    """
    Change a campaign's daily budget. THIS AFFECTS AD SPEND.

    DEFAULTS TO A DRY RUN. `amount` is in account currency units (e.g. dollars),
    converted to micros for the API. Budgets can be shared across campaigns —
    changing a shared budget affects every campaign that uses it.
    """
    if amount < 0:
        return "Budget amount cannot be negative."
    amount_micros = int(round(amount * 1_000_000))

    cid = format_customer_id(customer_id)
    op = {
        "updateMask": "amountMicros",
        "update": {
            "resourceName": f"customers/{cid}/campaignBudgets/{budget_id}",
            "amountMicros": str(amount_micros),
        },
    }
    return _run_mutate("campaignBudgets", customer_id, [op], confirm)


# ---------------------------------------------------------------------------
# Ad group status
# ---------------------------------------------------------------------------
@mcp.tool()
async def set_ad_group_status(
    customer_id: CustomerId,
    ad_group_id: Annotated[str, Field(description="Numeric ad group ID to update.")],
    status: StatusArg,
    confirm: ConfirmArg = False,
) -> str:
    """
    Enable, pause, or remove an ad group. DEFAULTS TO A DRY RUN.
    """
    try:
        status = _normalise_status(status)
    except ValueError as e:
        return str(e)

    cid = format_customer_id(customer_id)
    op = {
        "updateMask": "status",
        "update": {
            "resourceName": f"customers/{cid}/adGroups/{ad_group_id}",
            "status": status,
        },
    }
    return _run_mutate("adGroups", customer_id, [op], confirm)


# ---------------------------------------------------------------------------
# Ad status
# ---------------------------------------------------------------------------
@mcp.tool()
async def set_ad_status(
    customer_id: CustomerId,
    ad_group_id: Annotated[str, Field(description="Numeric ad group ID that contains the ad.")],
    ad_id: Annotated[str, Field(description="Numeric ad ID to update.")],
    status: StatusArg,
    confirm: ConfirmArg = False,
) -> str:
    """
    Enable, pause, or remove a single ad within an ad group. DEFAULTS TO A DRY RUN.

    The ad is addressed by the composite key ad_group_id~ad_id.
    """
    try:
        status = _normalise_status(status)
    except ValueError as e:
        return str(e)

    cid = format_customer_id(customer_id)
    op = {
        "updateMask": "status",
        "update": {
            "resourceName": f"customers/{cid}/adGroupAds/{ad_group_id}~{ad_id}",
            "status": status,
        },
    }
    return _run_mutate("adGroupAds", customer_id, [op], confirm)


# ---------------------------------------------------------------------------
# Keyword (ad group criterion) status
# ---------------------------------------------------------------------------
@mcp.tool()
async def set_keyword_status(
    customer_id: CustomerId,
    ad_group_id: Annotated[str, Field(description="Numeric ad group ID that contains the keyword.")],
    criterion_id: Annotated[str, Field(description="Numeric criterion ID (ad_group_criterion.criterion_id).")],
    status: StatusArg,
    confirm: ConfirmArg = False,
) -> str:
    """
    Enable, pause, or remove a keyword. DEFAULTS TO A DRY RUN.

    The keyword is addressed by the composite key ad_group_id~criterion_id.
    """
    try:
        status = _normalise_status(status)
    except ValueError as e:
        return str(e)

    cid = format_customer_id(customer_id)
    op = {
        "updateMask": "status",
        "update": {
            "resourceName": f"customers/{cid}/adGroupCriteria/{ad_group_id}~{criterion_id}",
            "status": status,
        },
    }
    return _run_mutate("adGroupCriteria", customer_id, [op], confirm)
