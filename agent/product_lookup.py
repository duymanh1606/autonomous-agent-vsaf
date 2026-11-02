"""Whether a reply may query the product catalogue in the database.

`products.lookup_enabled: true` keeps the existing behaviour: a product/stock
question is answered only from a verified business tool result, and if that
lookup fails the message goes to a human.

`products.lookup_enabled: false` turns the lookup off. The tool request the
analyzer produced is dropped before routing, so the message takes the ordinary
LLM path and is answered from the conversation context alone.

One instance is shared by the reply provider and the reply generators, so
flipping `enabled` at runtime changes both the routing and the prompt at once.
"""

import logging

from agent.tool_policy import without_tool
from models.analysis import MessageAnalysis


logger = logging.getLogger(__name__)

# Tools that read the product catalogue. Order, customer and shipping lookups
# are unaffected: this flag is about product information only.
PRODUCT_TOOLS = frozenset({"get_product", "check_inventory"})
PRODUCT_INTENT_TO_TOOL = {
    "ask_product": "get_product",
    "ask_price": "get_product",
    "check_inventory": "check_inventory",
}


class ProductLookupPolicy:
    def __init__(self, enabled: bool = True, tools=PRODUCT_TOOLS, *,
                 catalogue=None, default_product_id=None):
        self.enabled = bool(enabled)
        self.tools = frozenset(tools)
        # For a product question that names no product: the shop's default
        # product (`products.default_product_id`, else its only single item -
        # ProductRepository.default_product_id). Reassigned live by
        # control/settings.py's "products.default_product_id" setter.
        self.catalogue = catalogue
        self.default_product_id = default_product_id

    def default_product(self) -> str | None:
        resolve = getattr(self.catalogue, "default_product_id", None)
        if resolve is None:
            return None
        try:
            return resolve(self.default_product_id)
        except Exception:
            # No default is the safe answer: the agent asks which product.
            logger.warning("[products] resolving the default product failed", exc_info=True)
            return None

    def targets_product(self, analysis: MessageAnalysis) -> bool:
        return bool(analysis.requires_tool) and analysis.tool_name in self.tools

    def apply(self, analysis: MessageAnalysis) -> MessageAnalysis:
        """Apply the lookup switch and canonicalise product tool arguments.

        Gemini commonly identifies ``entities.product_name`` correctly but
        emits ``tool_args={"product_name": ...}``. The business surface uses
        one canonical argument, ``product_id``, which deliberately accepts an
        id *or* a searchable name. Normalising here keeps model wording out of
        service method signatures.

        With lookup enabled, a named product price/inventory question must use
        verified catalogue data even when the analyzer forgot to request a
        tool. Without that enforcement ``lookup_enabled: true`` was only a
        permission flag, not the guarantee documented in the configuration.
        """
        if not self.enabled:
            return without_tool(analysis) if self.targets_product(analysis) else analysis

        if analysis.requires_tool:
            # Unknown tool names are handled by the registry policy later. Do
            # not silently reinterpret (for example) `get_product_price` as a
            # real operation; only canonicalise a tool the model named exactly.
            requested_tool = (
                analysis.tool_name if analysis.tool_name in self.tools else None
            )
        else:
            requested_tool = (
                "get_product" if analysis.requested_media
                else PRODUCT_INTENT_TO_TOOL.get(analysis.intent)
            )
        if requested_tool not in self.tools:
            return analysis

        args = dict(analysis.tool_args or {})
        product_reference = next((
            str(value).strip()
            for value in (
                args.get("product_id"),
                args.get("product_name"),
                analysis.entities.product_id,
                analysis.entities.product_name,
            )
            if value is not None and str(value).strip()
        ), "")
        # No product named: in a shop that sells one product (or has set a
        # default) the question can only be about that one. Otherwise the
        # normal reply path asks which product the customer means instead of
        # calling a tool with no key.
        if not product_reference:
            product_reference = self.default_product() or ""
        if not product_reference:
            return analysis

        canonical_args = {"product_id": product_reference}
        if requested_tool == "check_inventory":
            size = next((
                str(value).strip()
                for value in (args.get("size"), analysis.entities.size)
                if value is not None and str(value).strip()
            ), "")
            if size:
                canonical_args["size"] = size

        return analysis.model_copy(update={
            "requires_tool": True,
            "tool_name": requested_tool,
            "tool_args": canonical_args,
        })
