"""What to do with a tool request the business registry cannot serve.

The analyzer names the business tool it wants in free text, so it can ask for
one that was never allowlisted or registered (`get_product_price` instead of
`get_product`). Such a request is a naming mistake, not a customer problem: the
tool request is dropped and the message takes the ordinary LLM path, which is
still guarded by `ReplyValidator`. Only a tool that exists and then *fails* is
a reason to hand the conversation to a human.
"""

from models.analysis import MessageAnalysis


def without_tool(analysis: MessageAnalysis) -> MessageAnalysis:
    """The same analysis, with its tool request removed."""
    if not analysis.requires_tool and analysis.tool_name is None:
        return analysis
    return analysis.model_copy(update={
        "requires_tool": False,
        "tool_name": None,
        "tool_args": {},
    })


def tool_is_available(business_tools, name: str | None) -> bool:
    """True when the registry can actually execute `name`."""
    if not name or business_tools is None:
        return False
    exists = getattr(business_tools, "exists", None)
    return bool(exists and exists(name))
